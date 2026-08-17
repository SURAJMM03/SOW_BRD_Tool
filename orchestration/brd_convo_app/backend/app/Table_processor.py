"""
Table Processing Module for Blueprint Document Generation
Converts Markdown table syntax to proper DOCX table objects.
"""

import re
from typing import List, Tuple
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def _shade_cell(cell, color: str):
    """Apply background color shading to a table cell."""
    try:
        shading_elm = OxmlElement('w:shd')
        shading_elm.set(qn('w:fill'), color)
        cell._element.get_or_add_tcPr().append(shading_elm)
    except Exception as e:
        print(f"Warning: Could not shade cell: {e}")


def _parse_markdown_table(table_text: str) -> Tuple[List[str], List[List[str]]]:
    """
    Parse a Markdown table and extract headers and rows.
    
    Args:
        table_text: Markdown table text (with | separators)
    
    Returns:
        Tuple of (headers, rows) where each row is a list of cells
    """
    lines = table_text.strip().split('\n')
    
    if len(lines) < 2:
        return None, None
    
    # Parse header row
    header_line = lines[0]
    headers = [cell.strip() for cell in header_line.split('|') if cell.strip()]
    
    # Skip separator line (should be line 1 with dashes)
    # Parse data rows
    rows = []
    for line in lines[2:]:  # Start from line 2 (after separator)
        if line.strip() and '|' in line:
            cells = [cell.strip() for cell in line.split('|') if cell.strip()]
            # Only include rows with correct number of cells
            if len(cells) == len(headers):
                rows.append(cells)
    
    return headers, rows if rows else None


def _find_tables_in_text(text: str) -> List[Tuple[int, int, str]]:
    """
    Find all Markdown tables in text.
    
    Returns:
        List of (start_pos, end_pos, table_text) tuples
    """
    # Pattern: | ... | followed by | --- | followed by | ... | rows
    pattern = r'\|(?:[^\n]*\|)+\n\|(?:\s*-+\s*\|)+\n(?:\|(?:[^\n]*\|)+\n)*'
    
    tables = []
    for match in re.finditer(pattern, text):
        tables.append((match.start(), match.end(), match.group(0)))
    
    return tables


def convert_text_with_tables_to_docx(text: str, template_doc: Document = None) -> Document:
    """
    Convert text with Markdown tables to a proper DOCX document.
    Creates real, editable Word tables.
    
    Args:
        text: Document text with Markdown tables
        template_doc: Optional template Document to use as base
    
    Returns:
        Document object with real tables inserted
    """
    if template_doc:
        doc = template_doc
    else:
        doc = Document()
    
    # Find all tables
    tables = _find_tables_in_text(text)
    
    if not tables:
        # No tables found, just add text as paragraphs
        for line in text.split('\n'):
            if line.strip():
                doc.add_paragraph(line)
        return doc
    
    # Process text by splitting at table boundaries
    last_pos = 0
    
    for start_pos, end_pos, table_text in tables:
        # Add text before table
        if start_pos > last_pos:
            text_before = text[last_pos:start_pos].strip()
            for line in text_before.split('\n'):
                if line.strip():
                    doc.add_paragraph(line)
        
        # Parse and add table
        headers, rows = _parse_markdown_table(table_text)
        
        if headers and rows:
            # Create table with (rows + 1 for header) and len(headers) columns
            table = doc.add_table(rows=len(rows) + 1, cols=len(headers))
            table.style = 'Table Grid'
            
            # Set column widths proportionally
            col_width = Inches(1.2)
            for row in table.rows:
                for cell in row.cells:
                    cell.width = col_width
            
            # Format header row
            header_cells = table.rows[0].cells
            for i, header_text in enumerate(headers):
                if i < len(header_cells):
                    header_cells[i].text = header_text
                    # Bold and center header
                    for paragraph in header_cells[i].paragraphs:
                        for run in paragraph.runs:
                            run.bold = True
                            run.font.size = Pt(11)
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    # Light blue background
                    _shade_cell(header_cells[i], 'D9E1F2')
            
            # Add data rows
            for row_idx, row_data in enumerate(rows):
                row_cells = table.rows[row_idx + 1].cells
                for col_idx, cell_text in enumerate(row_data):
                    if col_idx < len(row_cells):
                        row_cells[col_idx].text = cell_text
                        # Left align data cells
                        for paragraph in row_cells[col_idx].paragraphs:
                            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                            for run in paragraph.runs:
                                run.font.size = Pt(10)
        
        last_pos = end_pos
    
    # Add remaining text after last table
    if last_pos < len(text):
        text_after = text[last_pos:].strip()
        for line in text_after.split('\n'):
            if line.strip():
                doc.add_paragraph(line)
    
    return doc


def insert_table_into_docx(doc: Document, headers: List[str], rows: List[List[str]]):
    """
    Insert a properly formatted table into an existing DOCX document.
    
    Args:
        doc: Document object to insert table into
        headers: List of header cell texts
        rows: List of rows, where each row is a list of cell texts
    """
    if not headers or not rows:
        return
    
    # Create table
    table = doc.add_table(rows=len(rows) + 1, cols=len(headers))
    table.style = 'Table Grid'
    
    # Set column widths
    col_width = Inches(1.2)
    for row in table.rows:
        for cell in row.cells:
            cell.width = col_width
    
    # Format header row
    header_cells = table.rows[0].cells
    for i, header_text in enumerate(headers):
        if i < len(header_cells):
            header_cells[i].text = header_text
            # Bold and center header
            for paragraph in header_cells[i].paragraphs:
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(11)
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            # Light blue background
            _shade_cell(header_cells[i], 'D9E1F2')
    
    # Add data rows
    for row_idx, row_data in enumerate(rows):
        row_cells = table.rows[row_idx + 1].cells
        for col_idx, cell_text in enumerate(row_data):
            if col_idx < len(row_cells):
                row_cells[col_idx].text = cell_text
                # Left align data cells
                for paragraph in row_cells[col_idx].paragraphs:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    for run in paragraph.runs:
                        run.font.size = Pt(10)


# Test function
def test_table_parsing():
    """Test the table parsing and creation."""
    sample_markdown = """
| Functions | KPIs | Core Algorithms & Logic |
|-----------|------|------------------------| 
| Lead Time Management | On-time Delivery | Inclusive and cumulative lead time calculations |
| Safety Stock Planning | Inventory Turns | Fixed and time-phased safety stock policies |
"""
    
    headers, rows = _parse_markdown_table(sample_markdown)
    print(f"Headers: {headers}")
    print(f"Rows: {rows}")
    
    # Create a test document
    doc = Document()
    insert_table_into_docx(doc, headers, rows)
    doc.save('/tmp/test_table.docx')
    print("Test document saved to /tmp/test_table.docx")


if __name__ == '__main__':
    test_table_parsing()