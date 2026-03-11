import os
import pypdf
import logging
import io
from pathlib import Path
from django.core.files.base import ContentFile
from django.db import transaction
from django.conf import settings
from apps.documents.models import Document, Page
from common.enums import DocumentStatus, PageStatus
from common.utils import get_upload_path
from django.utils.timezone import now

logger = logging.getLogger(__name__)

class DocumentService:
    @staticmethod
    def create_document(client, file_obj):
        """
        Creates a new Document entry.
        """
        import os
        filename = file_obj.name
        ext = os.path.splitext(filename)[1].lower()
        from common.enums import OriginalFormat, ConversionStatus, DocumentStatus
        
        if ext == '.pdf':
            original_format = OriginalFormat.READABLE_PDF
            conversion_status = ConversionStatus.NOT_REQUIRED
        else:
            original_format = OriginalFormat.WORD_DOC
            conversion_status = ConversionStatus.PENDING

        doc = Document.objects.create(
            client=client,
            name=filename,
            original_file=file_obj,
            status=DocumentStatus.UPLOADED,
            original_format=original_format,
            conversion_status=conversion_status
        )
        print(f"DEBUG: Document record created: {doc.id}")
        
        # If PDF, we can use it directly for processing
        if ext == '.pdf':
            print("DEBUG: PDF detected. Setting file and READY status.")
            # Reuse the already saved stream to avoid redundant disk write
            doc.file = doc.original_file
            doc.status = DocumentStatus.READY
            doc.save()

        from apps.audit.models import AuditLog
        from common.enums import AuditEventType
        
        print("DEBUG: Creating AuditLog entry")
        AuditLog.objects.create(
            action=AuditEventType.DOC_UPLOADED,
            document_id=doc.id,
            actor=client,
            metadata={'filename': filename, 'extension': ext}
        )
        print("DEBUG: AuditLog created")
        return doc

    @staticmethod
    def split_document(document_id):
        """
        Splits a PDF document into individual pages.
        IDEMPOTENT: If pages already exist, skips creation.
        Uses fitz (PyMuPDF) for more reliable PDF handling.
        """
        import fitz
        from pathlib import Path
        from apps.processing.models import DocumentQueue
        from common.enums import QueueStatus, DocumentStatus, PipelineStatus
        from django.utils.timezone import now
        from django.core.files import File

        try:
            document = Document.objects.get(id=document_id)
            
            # Use converted_pdf if available, otherwise original_file
            pdf_file_field = document.file if document.file else document.original_file
            if not pdf_file_field or not Path(pdf_file_field.path).exists():
                raise FileNotFoundError(f"Processing file not found for document {document_id}")

            pdf = fitz.open(pdf_file_field.path)
            total = pdf.page_count
            document.total_pages = total
            document.pipeline_status = PipelineStatus.SPLITTING
            document.status = DocumentStatus.SPLITTING
            document.save(update_fields=['total_pages', 'pipeline_status', 'status'])

            errors = []
            pages_to_dispatch = []
            
            # Shared directory and timestamp for this batch
            current_date_path = now().strftime('%Y/%m/%d')
            base_out_dir = Path(settings.MEDIA_ROOT) / 'pages' / 'splits' / current_date_path / str(document_id)
            base_out_dir.mkdir(parents=True, exist_ok=True)

            from concurrent.futures import ThreadPoolExecutor
            
            # Pre-fetch existing pages
            existing_pages = {p.page_number: p for p in Page.objects.filter(document=document)}
            
            pages_to_create = []
            pages_to_update = []
            save_jobs = []

            def prepare_page(i):
                page_num = i + 1
                try:
                    # Extract single page
                    single_pdf = fitz.open()
                    single_pdf.insert_pdf(pdf, from_page=i, to_page=i)
                    buffer = io.BytesIO()
                    single_pdf.save(buffer)
                    single_pdf.close()
                    
                    filename = f'page_{page_num:04d}.pdf'
                    content = ContentFile(buffer.getvalue(), name=filename)

                    if page_num in existing_pages:
                        page = existing_pages[page_num]
                        page.status = PageStatus.PENDING
                    else:
                        page = Page(
                            document=document,
                            page_number=page_num,
                            status=PageStatus.PENDING
                        )
                    
                    # Store the content on the object, but don't save yet
                    return (page, filename, content)
                except Exception as e:
                    logger.error(f"Error extracting page {page_num}: {e}")
                    return None

            # 1. Extract pages in parallel (CPU/Memory bound)
            with ThreadPoolExecutor(max_workers=min(4, total)) as executor:
                results = list(executor.map(prepare_page, range(total)))

            # 2. Save file content to storage in parallel (I/O bound)
            # This is where the biggest speedup happens for network drives
            def save_to_storage(result):
                if not result: return None
                page, filename, content = result
                # Write to disk/storage
                page.content_file.save(filename, content, save=False)
                return page

            with ThreadPoolExecutor(max_workers=8) as executor:
                final_pages = list(executor.map(save_to_storage, results))

            # 3. Batch DB operations (Atomic)
            with transaction.atomic():
                for page in final_pages:
                    if not page: continue
                    if page.id: # Existing
                        pages_to_update.append(page)
                    else:
                        pages_to_create.append(page)

                if pages_to_create:
                    created_pages = Page.objects.bulk_create(pages_to_create)
                    pages_to_dispatch.extend([p.id for p in created_pages])
                
                if pages_to_update:
                    Page.objects.bulk_update(pages_to_update, ['content_file', 'status'])
                    pages_to_dispatch.extend([p.id for p in pages_to_update])

            pdf.close()

            # ✅ Verify integrity
            actual_count = Page.objects.filter(document=document).count()
            if actual_count != total:
                missing = total - actual_count
                document.pipeline_error = f"{missing} pages failed to split. Errors: {errors}"
                document.pipeline_status = PipelineStatus.FAILED
                document.status = DocumentStatus.FAILED
                document.save(update_fields=['pipeline_error', 'pipeline_status', 'status'])
                return

            # ✅ Section 3: Robust Pipeline Dispatch
            from apps.processing.tasks import (
                process_page_ocr_task, 
                mark_document_ready_to_assign
            )
            
            if getattr(settings, 'CELERY_TASK_ALWAYS_EAGER', False):
                logger.info(f"Running pipeline synchronously for document {document_id}")
                for pid in pages_to_dispatch:
                    try:
                        process_page_ocr_task(pid)
                    except Exception as e:
                        logger.error(f"OCR failed for page {pid}: {e}")
                mark_document_ready_to_assign(document.id)
            else:
                from celery import chain, group
                logger.info(f"Dispatching async pipeline for document {document_id}")
                pipeline = chain(
                    group(process_page_ocr_task.s(pid) for pid in pages_to_dispatch),
                    mark_document_ready_to_assign.si(document.id)
                )
                pipeline.apply_async()
            
            logger.info(f"Pipeline processing initiated for document {document_id}")

        except Exception as e:
            Document.objects.filter(id=document_id).update(status=DocumentStatus.FAILED, pipeline_error=str(e))
            raise e
