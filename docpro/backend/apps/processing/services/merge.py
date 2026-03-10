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
            if document.pipeline_status in [PipelineStatus.MERGED, PipelineStatus.APPROVED]:
                return
                
            total_pages = document.total_pages
            approved_submissions = SubmittedPage.objects.filter(
                document=document, 
                review_status=ReviewStatus.APPROVED
            ).order_by('page_number', '-submitted_at', '-id').distinct('page_number')
            
            if approved_submissions.count() != total_pages:
                raise ValueError(f"Cannot merge: only {approved_submissions.count()} of {total_pages} pages approved.")

            # 2. Get or Create MergedDocument tracking record
            merged_doc, _ = MergedDocument.objects.get_or_create(document=document)
            
            if merged_doc.merge_status == MergeStatus.COMPLETED:
                return
            
            try:
                # 3. Perform PyMuPDF Merge
                source_pdf_path = document.file.path
                doc_pdf = fitz.open(source_pdf_path)
                
                for submission in approved_submissions:
                    pdf_page_idx = submission.page_number - 1
                    if pdf_page_idx >= len(doc_pdf): 
                        continue
                        
                    pdf_page = doc_pdf[pdf_page_idx]
                    
                    if not submission.final_text:
                        continue
                        
                    soup = BeautifulSoup(submission.final_text, 'html.parser')
                    # Find all span and td tags with bboxes
                    elements = soup.find_all(['span', 'td'], attrs={'data-bbox': True})
                    
                    # Pass 1: Redact
                    for el in elements:
                        try:
                            bbox = json.loads(el['data-bbox'].replace('(', '[').replace(')', ']'))
                            if not bbox: continue
                            pdf_page.add_redact_annot(fitz.Rect(bbox), fill=(1,1,1))
                        except: continue
                        
                    pdf_page.apply_redactions()
                    
                    # Pass 2: Insert (Use insert_textbox for cells as it is more precise for single blocks)
                    for el in elements:
                        try:
                            bbox = json.loads(el['data-bbox'].replace('(', '[').replace(')', ']'))
                            if not bbox: continue
                            text_val = el.get_text().strip()
                            if text_val:
                                # Use standard sans-serif (helv) for maximum alignment stability
                                pdf_page.insert_textbox(
                                    fitz.Rect(bbox), 
                                    text_val,
                                    fontsize=10,
                                    fontname="helv",
                                    align=0
                                )
                        except: continue
                        
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

                # 6. Create ApprovedDocument record (The actual delivered output)
                ApprovedDocument.objects.create(
                    document=document,
                    merged_document=merged_doc,
                    approved_by_id=admin_user_id,
                    approval_notes="Auto-generated upon completion of all page reviews."
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
