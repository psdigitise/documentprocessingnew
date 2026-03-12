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
            page_index = 0
            if len(doc) > 1:
                page_index = page.page_number - 1
                if page_index < 0 or page_index >= len(doc):
                    page_index = 0
            
            source_page = doc[page_index]
            page_rect = source_page.rect
            doc.close()

            # 2. Create a NEW BLANK PDF for pure reconstruction
            # "The final PDF should not use the originally uploaded PDF. 
            # It must be generated only from the edited and submitted blocks."
            new_doc = fitz.open()
            pdf_page = new_doc.new_page(width=page_rect.width, height=page_rect.height)

            # 3. Extract elements to bake (prefer HTML text_content if available)
            elements = []
            if page.text_content:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(page.text_content, 'html.parser')
                # Find all span and td tags with bboxes (standard for our workspace)
                html_elements = soup.find_all(['span', 'td'], attrs={'data-bbox': True})
                for el in html_elements:
                    try:
                        bbox_str = el['data-bbox'].replace('(', '[').replace(')', ']')
                        bbox = json.loads(bbox_str)
                        if not bbox: continue
                        
                        text = el.get_text().strip()
                        # Default font info
                        font_size = 10.0
                        import re
                        style = el.get('style', '')
                        fs_match = re.search(r'font-size:\s*([\d\.]+)', style)
                        if fs_match:
                            font_size = float(fs_match.group(1))

                        elements.append({
                            'bbox': bbox,
                            'text': text,
                            'font_size': font_size
                        })
                    except: continue

            # Fallback to Block model if HTML is empty
            if not elements:
                for block in page.blocks.all():
                    elements.append({
                        'bbox': block.bbox,
                        'text': block.current_text or block.extracted_text,
                        'font_size': block.font_size or 10.0
                    })

            # 4. Insert User Text onto the BLANK page
            for el in elements:
                if not el['text']: continue
                
                # Use insert_textbox to place the text precisely where it was in the workspace
                pdf_page.insert_textbox(
                    fitz.Rect(el['bbox']),
                    el['text'],
                    fontsize=el['font_size'],
                    fontname="helv",
                    color=(0, 0, 0),
                    align=0
                )

            # 5. Save to buffer
            result_buffer = BytesIO()
            new_doc.save(result_buffer)
            new_doc.close()

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
