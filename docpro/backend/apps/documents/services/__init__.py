
import os
import pypdf
from django.core.files.base import ContentFile
from django.db import transaction
from django.conf import settings
from apps.documents.models import Document, Page
from common.enums import DocumentStatus, PageStatus
from common.utils import get_upload_path

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
            doc.file = file_obj
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

            created_count = 0
            errors = []
            pages_to_dispatch = []

            # ✅ Idempotent extraction (using update_or_create to prevent duplicates)
            for i in range(total):
                page_num = i + 1
                try:
                    # Extract single page as new PDF
                    single_pdf = fitz.open()
                    single_pdf.insert_pdf(pdf, from_page=i, to_page=i)

                    out_dir = Path(settings.MEDIA_ROOT) / 'pages' / 'splits' / now().strftime('%Y/%m/%d') / str(document_id)
                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_path = out_dir / f'page_{page_num:04d}.pdf'
                    
                    single_pdf.save(str(out_path))
                    single_pdf.close()

                    # Save using update_or_create to avoid duplicates on retries
                    with open(out_path, 'rb') as f:
                        django_file = File(f, name=out_path.name)
                        page, created = Page.objects.update_or_create(
                            document=document,
                            page_number=page_num,
                            defaults={
                                'content_file': django_file,
                                'status': PageStatus.PENDING
                            }
                        )
                        pages_to_dispatch.append(page.id)

                except Exception as e:
                    errors.append({'page': page_num, 'error': str(e)})

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

            # ✅ Section 3: Robust Pipeline Dispatch (group + chain)
            from celery import chain, group
            from apps.processing.tasks import (
                process_page_ocr_task, 
                mark_document_ready_to_assign
            )
            
            # Chain: 
            # 1. Process all pages in parallel (OCR -> Validation -> Score)
            # 2. Once ALL done, mark document ready for assignment
            pipeline = chain(
                group(process_page_ocr_task.s(pid) for pid in pages_to_dispatch),
                mark_document_ready_to_assign.si(document.id)
            )
            pipeline.apply_async()
            
            logger.info(f"Pipeline dispatched for document {document_id}")

        except Exception as e:
            Document.objects.filter(id=document_id).update(status=DocumentStatus.FAILED, pipeline_error=str(e))
            raise e
