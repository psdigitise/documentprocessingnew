import json
import logging
import fitz
from bs4 import BeautifulSoup
from io import BytesIO

from django.db import transaction
from django.utils import timezone
from django.core.files.base import ContentFile

from apps.documents.models import Document
from apps.processing.models import SubmittedPage, MergedDocument, ApprovedDocument
from common.enums import PipelineStatus, ReviewStatus, MergeStatus, DocumentStatus

logger = logging.getLogger(__name__)

class MergeService:
    @staticmethod
    def merge_approved_pages(document: Document, admin_user_id=None):
        """
        Takes all APPROVED SubmittedPage records for a Document and merges them 
        into a final PDF. (Section 10).
        Creates MergedDocument and ApprovedDocument records.
        """
        with transaction.atomic():
            document = Document.objects.select_for_update().get(id=document.id)
            
            # 1. Validation check
            if document.pipeline_status == PipelineStatus.APPROVED and document.final_file:
                return
                
            total_pages = document.total_pages or 0
            if total_pages == 0:
                raise ValueError("Document has no pages recorded.")

            approved_qs = SubmittedPage.objects.filter(
                document=document, 
                review_status=ReviewStatus.APPROVED
            ).order_by('page_number', '-submitted_at', '-id').distinct('page_number')
            
            approved_count = approved_qs.count()
            if approved_count != total_pages:
                # Find the gaps for better error reporting
                found_pages = set(approved_qs.values_list('page_number', flat=True))
                expected_pages = set(range(1, total_pages + 1))
                missing = sorted(list(expected_pages - found_pages))
                raise ValueError(f"Cannot merge: {approved_count}/{total_pages} pages approved. Missing: {missing}")

            # 2. Get or Create MergedDocument tracking record (Idempotent)
            merged_doc, _ = MergedDocument.objects.get_or_create(document=document)
            
            try:
                # 3. Perform PyMuPDF Merge (Strict Order Enforcement)
                doc_pdf = fitz.open() # Create a blank PDF
                
                # We iterate based on the ORIGINAL document sequence to ensure "Correct Order"
                for page_num in range(1, total_pages + 1):
                    submission = approved_qs.filter(page_number=page_num).first()
                    
                    if not submission:
                        # This shouldn't happen due to the count check above, but for belt-and-suspenders:
                        raise ValueError(f"Integrity Error: Approved submission for page {page_num} went missing during merge.")
                    
                    if not submission.output_page_file:
                        logger.warning(f"Page {page_num} has no output_page_file. Attempting to bake now.")
                        from apps.processing.services.pdf_baking import PDFBakeService
                        baked_content = PDFBakeService.bake_page_edits(submission.page)
                        filename = f"on_the_fly_p{page_num}_{submission.id}.pdf"
                        submission.output_page_file.save(filename, ContentFile(baked_content))
                    
                    # Open the baked submitted page and append to the final PDF
                    # Using .open() instead of .path for durability across environments
                    with fitz.open(stream=submission.output_page_file.read(), filetype="pdf") as page_pdf:
                        doc_pdf.insert_pdf(page_pdf)
                        
                # 4. Save to buffer
                result_buffer = BytesIO()
                doc_pdf.save(result_buffer)
                doc_pdf.close()
                
                from django.core.files.base import ContentFile
                filename = f"final_merged_{document.doc_ref}.pdf"
                merged_doc.merged_file.save(filename, ContentFile(result_buffer.getvalue()), save=False)
                
                merged_doc.merge_status = MergeStatus.COMPLETED
                merged_doc.merge_completed_at = timezone.now()
                # Assuming admin_user_id is passed if triggered by API
                merged_doc.merged_by_id = admin_user_id 
                merged_doc.save()

                # 6. Create/Update ApprovedDocument record
                ApprovedDocument.objects.update_or_create(
                    document=document,
                    defaults={
                        'merged_document': merged_doc,
                        'approved_by_id': admin_user_id,
                        'approval_notes': "Auto-generated upon completion of all page reviews."
                    }
                )
                
                # 7. Create Audit Log Entry
                from apps.audit.models import AuditLog
                from common.enums import AuditEventType
                AuditLog.objects.create(
                    action=AuditEventType.DOC_COMPLETED,
                    document_id=document.id,
                    actor_id=admin_user_id,
                    metadata={'page_count': total_pages}
                )
                
                # 7. Update Document Status
                document.final_file.save(filename, ContentFile(result_buffer.getvalue()), save=False)
                document.pipeline_status = PipelineStatus.MERGED
                document.status = DocumentStatus.COMPLETED
                document.completed_at = timezone.now()
                document.save(update_fields=['final_file', 'pipeline_status', 'status', 'completed_at'])
                
            except Exception as e:
                document.pipeline_status = PipelineStatus.FAILED
                document.pipeline_error = f"Merge failed: {str(e)}"
                document.save(update_fields=['pipeline_status', 'pipeline_error'])
                logger.error(f"Error merging document {document.id}: {e}")
                raise e
