import fitz  # PyMuPDF
import cv2
import pytesseract
import numpy as np
import os
import tempfile
import json
from pytesseract import Output
from django.conf import settings

# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'  # Might be needed on Windows

class OCRService:

    @classmethod
    def get_tesseract_cmd(cls):
        # Set explicitly for Windows if not in PATH
        possible_paths = [
            r'C:\Program Files\Tesseract-OCR\tesseract.exe',
            r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
            'tesseract'
        ]
        for path in possible_paths:
            if os.path.exists(path):
                return path
        return 'tesseract'

    @classmethod
    def process_page(cls, page_obj):
        """
        Takes a Page model object, detects if it has text, and if not, runs Tesseract OCR.
        Saves the layout as JSON and structured HTML to the page_obj.
        """
        pytesseract.pytesseract.tesseract_cmd = cls.get_tesseract_cmd()
        file_path = page_obj.content_file.path
        
        doc = fitz.open(file_path)
        if len(doc) == 0:
            return

        page = doc[0]
        # Detect if PDF is native by checking if it has non-trivial text
        native_text = page.get_text("text").strip()

        layout = None
        if len(native_text) > 30: # Native PDF Path
            layout = cls._extract_native_layout(page)
            page_obj.is_scanned = False
        else:
            # Scanned PDF Path (OCR)
            layout, _ = cls._extract_ocr_layout(page)
            page_obj.is_scanned = True

        # Reconstruct HTML Content for Editor (Enterprise Phase 8: Word-Level)
        html_blocks = []
        for block in layout.get("blocks", []):
            # If the block has granular lines/spans (Native PDF), use them
            if block.get("lines"):
                for line in block["lines"]:
                    for span in line["spans"]:
                        bbox_str = json.dumps(span['bbox'])
                        # Span-level precision
                        html_blocks.append(f'<span data-bbox=\'{bbox_str}\' style="font-family: {span.get("font", "sans-serif")}; font-size: {span.get("size", 10)}pt;">{span["text"]}</span>')
            else:
                # Fallback for simple blocks or OCR paths
                bbox_str = json.dumps(block['bbox'])
                html_blocks.append(f'<p data-bbox=\'{bbox_str}\'>{block.get("text", "")}</p>')
        
        for table in layout.get("tables", []):
            # Reconstruct table grid for editor
            rows = {}
            for cell in table["cells"]:
                r_idx = cell["row_index"]
                if r_idx not in rows: rows[r_idx] = []
                
                # Add data-bbox to cells for precise merging
                bbox_str = json.dumps(cell['bbox']) if cell.get('bbox') else "{}"
                rows[r_idx].append(f"<td data-bbox='{bbox_str}'>{str(cell['text']).strip()}</td>")
            
            table_bbox_str = json.dumps(table['bbox'])
            table_html = f'<table class="pdf-table" data-bbox=\'{table_bbox_str}\'>'
            for r in sorted(rows.keys()):
                table_html += f"<tr>{''.join(rows[r])}</tr>"
            table_html += '</table>'
            html_blocks.append(table_html)
        
        structured_html = "".join(html_blocks)

        # Save results
        page_obj.layout_data = layout
        page_obj.text_content = structured_html
        page_obj.is_processed = True
        page_obj.save(update_fields=[
            'layout_data', 'text_content', 'is_processed', 'is_scanned'
        ])
        doc.close()

    @classmethod
    def _extract_native_layout(cls, page):
        """ 
        ENTERPRISE LAYER 1: Extracts granular word-level coordinates, 
        fonts, and tables for high-fidelity correction.
        """
        import pdfplumber
        width = page.rect.width
        height = page.rect.height
        reconstructed = {
            "page_dims": {"width": width, "height": height},
            "blocks": [],
            "tables": []
        }
        
        # 1. Advanced Table Detection (fitz)
        tabs = page.find_tables()
        table_bboxes = [list(t.bbox) for t in tabs]
        
        # 2. Extract Detailed Dictionary (fitz)
        layout_dict = page.get_text("dict")
        
        for block in layout_dict.get("blocks", []):
            if block["type"] == 0: # Text block
                bbox = block["bbox"]
                
                # Check table overlap
                in_table = any(cls._rect_overlap(bbox, t_bbox) for t_bbox in table_bboxes)
                if in_table: continue
                
                # Capture Word-Level precision within lines
                lines_data = []
                for line in block["lines"]:
                    spans_data = []
                    for span in line["spans"]:
                        spans_data.append({
                            "text": span["text"],
                            "bbox": span["bbox"],
                            "font": span["font"],
                            "size": span["size"],
                            "color": span["color"]
                        })
                    lines_data.append({
                        "bbox": line["bbox"],
                        "spans": spans_data
                    })
                
                reconstructed["blocks"].append({
                    "type": "paragraph",
                    "bbox": list(bbox),
                    "lines": lines_data,
                    "text": " ".join([s["text"] for l in lines_data for s in l["spans"]])
                })

        # 3. Structured Table Extraction
        for table in tabs:
            cell_data = []
            # fitz.Table.cells is a list of bboxes roughly in row-major order
            # matching the extract() grid
            try:
                raw_extract = table.extract()
                cell_bboxes = table.cells # List of (x0, y0, x1, y1)
                
                cell_idx = 0
                for r_idx, row in enumerate(raw_extract):
                    for c_idx, cell_text in enumerate(row):
                        bbox = None
                        if cell_idx < len(cell_bboxes):
                            bbox = list(cell_bboxes[cell_idx])
                        
                        cell_data.append({
                            "text": str(cell_text or ""),
                            "row_index": r_idx,
                            "col_index": c_idx,
                            "bbox": bbox
                        })
                        cell_idx += 1
            except Exception as e:
                print(f"Error extracting table cells: {e}")

            reconstructed["tables"].append({
                "bbox": list(table.bbox),
                "cells": cell_data
            })
            
        return reconstructed

    @staticmethod
    def _rect_overlap(rect1, rect2):
        """ Simple overlap check for bboxes """
        return not (rect1[2] < rect2[0] or rect1[0] > rect2[2] or
                    rect1[3] < rect2[1] or rect1[1] > rect2[3])

    @classmethod
    def _extract_ocr_layout(cls, page):
        """ Renders page, preprocesses with OpenCV, and runs Tesseract OCR Layout reconstruction """
        pix = cls._generate_low_dpi_pixmap(page)
        img_data = pix.tobytes("png")
        
        # Convert to OpenCV Image
        nparr = np.frombuffer(img_data, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

        # Preprocessing: Grayscale -> Threshold -> Deskew
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Adaptive thresholding to binarize
        thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
        
        # Deskewing disabled to ensure 1:1 alignment with original PDF background
        # if abs(angle) > 0.5:
        #     (h, w) = img.shape[:2]
        #     center = (w // 2, h // 2)
        #     M = cv2.getRotationMatrix2D(center, angle, 1.0)
        #     img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        #     gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Run Tesseract with Dictionaries
        custom_config = r'--oem 1 --psm 3 -l eng'
        ocr_data = pytesseract.image_to_data(gray, config=custom_config, output_type=Output.DICT)

        # Reconstruct Layout
        height, width = gray.shape
        reconstructed = {
            "page_dims": {"width": width, "height": height},
            "blocks": [],
            "tables": []
        }

        n_boxes = len(ocr_data['level'])
        current_block = []
        current_block_bbox = None
        full_text = []

        for i in range(n_boxes):
            text = ocr_data['text'][i].strip()
            if not text:
                continue

            # Valid text
            x, y, w, h = (ocr_data['left'][i], ocr_data['top'][i], ocr_data['width'][i], ocr_data['height'][i])
            
            # FIX: Scale OCR pixel coordinates back to PDF points (72 DPI)
            # Tesseract was run on a 300/150 DPI image.
            # PDF points = (pixels / rendered_dpi) * 72
            rendered_dpi = 300 if width > 2000 else 150 # Heuristic based on _generate_low_dpi_pixmap
            scale_to_pt = 72.0 / rendered_dpi
            
            x, y, w, h = x * scale_to_pt, y * scale_to_pt, w * scale_to_pt, h * scale_to_pt
            
            # Basic line/paragraph logic (Tesseract gives block_num/line_num)
            block_num = ocr_data['block_num'][i]
            
            # Store block data
            if len(current_block) > 0 and current_block[-1]['block_num'] != block_num:
                # Save previous block
                x0 = min([w['x'] for w in current_block])
                y0 = min([w['y'] for w in current_block])
                x1 = max([w['x'] + w['w'] for w in current_block])
                y1 = max([w['y'] + w['h'] for w in current_block])
                
                block_text = " ".join([w['text'] for w in current_block])
                full_text.append(block_text)
                
                reconstructed["blocks"].append({
                    "text": block_text,
                    "bbox": [x0, y0, x1, y1],
                    "type": "paragraph"
                })
                current_block = []
            
            current_block.append({
                "text": text,
                "x": x, "y": y, "w": w, "h": h,
                "block_num": block_num,
                "line_num": ocr_data['line_num'][i]
            })

        # Append last block
        if current_block:
            cls._save_ocr_block(reconstructed, current_block, full_text)

        # Sort blocks by Y position
        if reconstructed["blocks"]:
            reconstructed["blocks"].sort(key=lambda b: (round(b["bbox"][1]/10), b["bbox"][0]))

        return reconstructed, ""

    @classmethod
    def _save_ocr_block(cls, reconstructed, current_block, full_text):
        """ Helper to save grouped OCR words as a structured block """
        x0 = min([w['x'] for w in current_block])
        y0 = min([w['y'] for w in current_block])
        x1 = max([w['x'] + w['w'] for w in current_block])
        y1 = max([w['y'] + w['h'] for w in current_block])
        
        # Group by line_num for internal breaks
        lines = {}
        for w in current_block:
            l_num = w['line_num']
            if l_num not in lines: lines[l_num] = []
            lines[l_num].append(w['text'])
        
        block_text_lines = [" ".join(lines[ln]) for ln in sorted(lines.keys())]
        block_html = "<br>".join(block_text_lines)
        
        reconstructed["blocks"].append({
            "text": block_html,
            "bbox": [x0, y0, x1, y1],
            "type": "paragraph"
        })

    @classmethod
    def _generate_low_dpi_pixmap(cls, page):
        """ Memory-safe fallback for large PDFs """
        try:
            return page.get_pixmap(dpi=300)
        except Exception:
            return page.get_pixmap(dpi=150) # Fallback to lower resolution
