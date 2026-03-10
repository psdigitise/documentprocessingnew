import fitz
import logging
import json
from io import BytesIO
from django.core.files.base import ContentFile
from apps.documents.models import Page

logger = logging.getLogger(__name__)

class PDFBakeService:
    @staticmethod
    def bake_page_edits(page: Page):
        """
        Takes a Page and its edited Blocks, and produces a 'baked' PDF page version.
        This overwrites the original text on the page with user edits.
        """
        try:
            # 1. Get source PDF path
            # If it's a split page, use its own content_file. Otherwise use the document's file.
            if page.content_file:
                source_pdf = page.content_file
            else:
                source_pdf = page.document.file
                
            if not source_pdf:
                raise ValueError(f"No source PDF found for page {page.id}")

            # Open from bytes to avoid path issues with split files in memory
            doc = fitz.open(stream=source_pdf.read(), filetype="pdf")
            
            # Determine which page in the PDF we are targeting
            # If it's already a single-page split PDF, then it's index 0
            # If it's the original full document, it's page_number - 1
            if page.content_file and 'assignments/' in page.content_file.name:
                pdf_page = doc[0]
            else:
                pdf_page = doc[page.page_number - 1]

            blocks = page.blocks.all()
            
            # Pass 1: Redact original blocks
            for block in blocks:
                bbox = block.bbox
                # fitz.Rect expects [x0, y0, x1, y1]
                pdf_page.add_redact_annot(fitz.Rect(bbox), fill=(1,1,1))
            
            pdf_page.apply_redactions()

            # Pass 2: Insert User Text
            for block in blocks:
                text = block.current_text # This is the user's latest edit
                if not text: 
                    text = block.extracted_text
                
                if not text: continue
                
                bbox = block.bbox
                # Use fitz's textbox for precision
                # Note: PyMuPDF coords are 72 DPI (Points)
                pdf_page.insert_textbox(
                    fitz.Rect(bbox),
                    text,
                    fontsize=block.font_size,
                    fontname="helv", # Default to helvetica if unknown
                    color=(0, 0, 0), # Primary black
                    align=0 # left
                )

            # 3. Save to buffer
            result_buffer = BytesIO()
            doc.save(result_buffer)
            doc.close()

            return result_buffer.getvalue()
        except Exception as e:
            logger.error(f"Failed to bake page {page.id}: {e}", exc_info=True)
            raise e

    @staticmethod
    def sync_baked_file(page: Page):
        """Generates and saves the baked PDF to a temporary preview field or replaces content_file if appropriate."""
        # For now, let's create a naming convention for baked previews
        content = PDFBakeService.bake_page_edits(page)
        filename = f"baked_p{page.page_number}_{page.id}.pdf"
        
        # We could save this to a 'preview_file' field if we had one, 
        # or just return it for real-time serving.
        return content
