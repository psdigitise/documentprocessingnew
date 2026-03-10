import fitz          # PyMuPDF — block coordinates
import pdfplumber    # table structure detection
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Data structures ───────────────────────────────────────────

@dataclass
class TextBlock:
    """Single text block with full coordinate data."""
    block_id:    str
    page_id:     int
    text:        str
    x:           float   # left position in PDF points
    y:           float   # top position in PDF points
    width:       float
    height:      float
    font_size:   float   = 11.0
    font_family: str     = 'serif'
    font_weight: str     = 'normal'  # 'bold' | 'normal'
    font_style:  str     = 'normal'  # 'italic' | 'normal'
    color:       str     = '#000000'
    block_type:  str     = 'text'    # 'text' | 'table_cell' | 'header'
    table_id:    str = ''
    row_index:   Optional[int] = None
    col_index:   Optional[int] = None


@dataclass
class TableStructure:
    """Detected table with rows and columns."""
    table_id:   str
    page_id:    int
    x:          float
    y:          float
    width:      float
    height:     float
    rows:       List[List[str]]          # [row][col] = text
    col_widths: List[float]              # width per column
    row_heights: List[float]             # height per row
    has_borders: bool = True
    col_count:  int   = 0
    row_count:  int   = 0


# ── Main extraction class ──────────────────────────────────────

class PDFLayoutEngine:
    """
    Dual-engine PDF layout extractor.
    Uses pdfplumber for table detection.
    Uses PyMuPDF for precise block coordinates + font info.
    Combines both for complete layout data.
    """

    # Y-axis tolerance for grouping text on the same row (pts)
    ROW_Y_TOLERANCE  = 3.0
    # X-axis gap that indicates a new column
    COL_GAP_THRESHOLD = 20.0
    # Minimum chars to consider a block non-empty
    MIN_TEXT_LENGTH  = 1

    def extract_page_layout(
        self,
        pdf_path: str,
        page_index: int = 0   # 0-based
    ) -> dict:
        """
        Full layout extraction for one page.
        Returns blocks list + tables list + page dimensions.
        """
        result = {
            'page_index':  page_index,
            'page_width':  0,
            'page_height': 0,
            'blocks':      [],
            'tables':      [],
            'has_tables':  False,
        }

        # ── Engine 1: PyMuPDF for coordinates + fonts ──────────
        fitz_blocks = self._extract_fitz_blocks(
            pdf_path, page_index, result
        )

        # ── Engine 2: pdfplumber for table structure ───────────
        plumber_tables = self._extract_plumber_tables(
            pdf_path, page_index
        )

        # ── Combine: mark blocks that belong to tables ─────────
        if plumber_tables:
            result['has_tables'] = True
            result['tables']     = plumber_tables
            fitz_blocks = self._tag_table_blocks(
                fitz_blocks, plumber_tables
            )

        result['blocks'] = [asdict(b) for b in fitz_blocks]
        return result

    # ── PyMuPDF extraction ─────────────────────────────────────

    def _extract_fitz_blocks(
        self,
        pdf_path: str,
        page_index: int,
        result: dict
    ) -> List[TextBlock]:
        """
        Extract all text blocks with coordinates using PyMuPDF.
        Uses get_text('rawdict') for full font information.
        """
        blocks_out = []

        try:
            doc  = fitz.open(pdf_path)
            page = doc[page_index]

            result['page_width']  = page.rect.width
            result['page_height'] = page.rect.height

            # rawdict gives per-character font info
            raw = page.get_text('rawdict', flags=(
                fitz.TEXT_PRESERVE_WHITESPACE
                | fitz.TEXT_PRESERVE_LIGATURES
            ))

            block_idx = 0
            for block in raw.get('blocks', []):
                if block.get('type') != 0:  # 0 = text block
                    continue

                for line in block.get('lines', []):
                    # Collect all spans in this line
                    line_text  = ''
                    font_size  = 11.0
                    font_family = 'serif'
                    font_weight = 'normal'
                    font_style  = 'normal'
                    color       = '#000000'

                    spans = line.get('spans', [])
                    if not spans:
                        continue

                    # Get bbox from first span, extend across all spans
                    all_x0 = [s['bbox'][0] for s in spans]
                    all_x1 = [s['bbox'][2] for s in spans]
                    all_y0 = [s['bbox'][1] for s in spans]
                    all_y1 = [s['bbox'][3] for s in spans]

                    x0 = min(all_x0)
                    y0 = min(all_y0)
                    x1 = max(all_x1)
                    y1 = max(all_y1)

                    for span in spans:
                        span_text = span.get('text')
                        if span_text is None:
                            # Reconstruct from chars if text is missing in rawdict
                            span_text = "".join([c.get('c', '') for c in span.get('chars', [])])
                        
                        line_text += span_text
                        font_size  = span.get('size', 11.0)

                        # Decode font flags
                        flags = span.get('flags', 0)
                        font_weight = 'bold'   if flags & 2**4 else 'normal'
                        font_style  = 'italic' if flags & 2**1 else 'normal'

                        # Decode font family
                        font_name = span.get('font', 'serif').lower()
                        if any(f in font_name for f in ['times', 'serif', 'georgia']):
                            font_family = 'serif'
                        elif any(f in font_name for f in ['arial','helvetica','sans']):
                            font_family = 'sans-serif'
                        elif any(f in font_name for f in ['courier','mono','code']):
                            font_family = 'monospace'

                        # Decode color (PyMuPDF stores as int)
                        raw_color = span.get('color', 0)
                        if isinstance(raw_color, int):
                            r = (raw_color >> 16) & 0xFF
                            g = (raw_color >> 8)  & 0xFF
                            b = raw_color         & 0xFF
                            color = f'#{r:02x}{g:02x}{b:02x}'

                    text = line_text.strip()
                    if len(text) < self.MIN_TEXT_LENGTH:
                        continue

                    blocks_out.append(TextBlock(
                        block_id    = f'b_{page_index}_{block_idx}',
                        page_id     = page_index,
                        text        = text,
                        x           = round(x0, 2),
                        y           = round(y0, 2),
                        width       = round(x1 - x0, 2),
                        height      = round(y1 - y0, 2),
                        font_size   = round(font_size, 1),
                        font_family = font_family,
                        font_weight = font_weight,
                        font_style  = font_style,
                        color       = color,
                    ))
                    block_idx += 1

            doc.close()

        except Exception as e:
            logger.error(
                f'[Extract] fitz failed page={page_index}: {e}',
                exc_info=True
            )

        return blocks_out

    # ── pdfplumber table extraction ────────────────────────────

    def _extract_plumber_tables(
        self,
        pdf_path: str,
        page_index: int
    ) -> List[dict]:
        """
        Use pdfplumber to detect and extract table structure.
        Returns list of tables with rows/columns/coordinates.
        """
        tables_out = []

        try:
            with pdfplumber.open(pdf_path) as pdf:
                if page_index >= len(pdf.pages):
                    return []

                page = pdf.pages[page_index]

                # pdfplumber table settings for financial documents
                table_settings = {
                    'vertical_strategy':   'lines',   # use drawn lines
                    'horizontal_strategy': 'lines',
                    'snap_tolerance':      3,
                    'join_tolerance':      3,
                    'edge_min_length':     3,
                    'min_words_vertical':  1,
                    'min_words_horizontal': 1,
                    'intersection_tolerance': 3,
                    'text_tolerance':      3,
                }

                extracted_tables = page.extract_tables(table_settings)
                table_bboxes     = [
                    t.bbox for t in page.find_tables(table_settings)
                ] if hasattr(page, 'find_tables') else []

                for i, table_data in enumerate(extracted_tables):
                    if not table_data:
                        continue

                    # Get bbox for this table
                    bbox = table_bboxes[i] if i < len(table_bboxes) else None

                    # Calculate column widths from content
                    col_count = max(len(row) for row in table_data) if table_data else 0
                    row_count = len(table_data)

                    # Normalize rows — fill missing cells with empty string
                    normalized_rows = []
                    for row in table_data:
                        norm_row = [
                            (cell or '').strip()
                            for cell in row
                        ]
                        # Pad to col_count
                        while len(norm_row) < col_count:
                            norm_row.append('')
                        normalized_rows.append(norm_row)

                    table_id = f'table_{page_index}_{i}'

                    tables_out.append({
                        'table_id':   table_id,
                        'page_id':    page_index,
                        'x':          bbox[0] if bbox else 0,
                        'y':          bbox[1] if bbox else 0,
                        'width':      (bbox[2] - bbox[0]) if bbox else 500,
                        'height':     (bbox[3] - bbox[1]) if bbox else 100,
                        'rows':       normalized_rows,
                        'col_count':  col_count,
                        'row_count':  row_count,
                        'has_borders': True,
                    })

        except Exception as e:
            logger.warning(
                f'[Extract] pdfplumber table extraction failed '
                f'page={page_index}: {e}'
            )

        return tables_out

    # ── Tag blocks that are inside detected tables ─────────────

    def _tag_table_blocks(
        self,
        blocks: List[TextBlock],
        tables: List[dict]
    ) -> List[TextBlock]:
        """
        Mark each text block with table_id, row_index, col_index
        if it falls within a detected table region.
        """
        for block in blocks:
            for table in tables:
                tx, ty = table['x'], table['y']
                tw, th = table['width'], table['height']

                # Check if block center is inside table bounds
                bx_center = block.x + block.width / 2
                by_center = block.y + block.height / 2

                if (tx <= bx_center <= tx + tw
                        and ty <= by_center <= ty + th):
                    block.block_type = 'table_cell'
                    block.table_id   = table['table_id']
                    break

        return blocks


# ── Scale coordinates from PDF points to CSS pixels ──────────

def scale_coords(value: float, pdf_dim: float, css_dim: float) -> float:
    """
    Scale a coordinate from PDF point space to CSS pixel space.
    pdf_dim: PDF page width or height in points
    css_dim: CSS container width or height in pixels
    """
    if pdf_dim == 0:
        return value
    return round((value / pdf_dim) * css_dim, 2)


def coords_to_css(
    block: dict,
    pdf_width: float,
    pdf_height: float,
    css_width: float,
    css_height: float
) -> dict:
    """Convert PDF coordinates to CSS pixel values."""
    scale_x = css_width  / pdf_width  if pdf_width  else 1
    scale_y = css_height / pdf_height if pdf_height else 1

    return {
        **block,
        'css_left':      round(block['x']      * scale_x, 2),
        'css_top':       round(block['y']      * scale_y, 2),
        'css_width':     round(block['width']  * scale_x, 2),
        'css_height':    round(block['height'] * scale_y, 2),
        'css_font_size': round(block['font_size'] * scale_y, 2),
        'scale_x':       scale_x,
        'scale_y':       scale_y,
    }
