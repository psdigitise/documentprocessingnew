import logging
from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from django.db import transaction

from apps.processing.services.core import AssignmentService
from apps.processing.services.validator import PageValidator
from apps.processing.services.complexity import ComplexityScorer
from apps.documents.models import Document, Page
from apps.processing.models import PageAssignment
from apps.accounts.models import ResourceProfile
from common.enums import (
    PageStatus, PipelineStatus, PageAssignmentStatus,
    ResourceStatus, ValidationStatus, ComplexityType
)

logger = logging.getLogger(__name__)

# ── Section 1: Resource Heartbeat Expiry ───────────────────
@shared_task(name='tasks.mark_inactive_resources')
def mark_inactive_resources():
    """
    Runs every 60 seconds via Celery Beat.
    Marks resources as INACTIVE if no heartbeat for > 2 minutes (120s).
    """
    from django.utils import timezone
    from datetime import timedelta
    from apps.accounts.models import ResourceProfile
    from common.enums import ResourceStatus

    cutoff = timezone.now() - timedelta(seconds=120)

    gone_offline = ResourceProfile.objects.filter(
        status__in=[ResourceStatus.ACTIVE, ResourceStatus.BUSY],
        last_seen__lt=cutoff
    )

    count = gone_offline.count()
    if count > 0:
        gone_offline.update(status=ResourceStatus.INACTIVE, is_available=False)
        logger.info(f"Marked {count} resources INACTIVE due to missed heartbeats (120s timeout).")

    return {'marked_inactive': count}


# ── Section 2: Processing Time Limits ──────────────────────
@shared_task
def check_processing_timeouts():
    """
    Runs every 60 seconds.
    Checks for IN_PROGRESS assignments approaching or exceeding SLA.
    8 mins = send warning via WebSocket
    10 mins = revoke, reassign, mark TIMED_OUT
    """
    active_assignments = PageAssignment.objects.filter(
        status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
    ).select_related('resource__user', 'page')

    now = timezone.now()
    warnings_sent = 0
    timeouts_triggered = 0

    for assignment in active_assignments:
        # Calculate elapsed from start if available, otherwise from assignment time
        start_time = assignment.processing_start_at or assignment.assigned_at
        elapsed = (now - start_time).total_seconds()
        
        # 10 minute timeout (600s)
        if elapsed >= assignment.max_processing_time:
            with transaction.atomic():
                # 1. Update assignment
                assignment.status = PageAssignmentStatus.TIMED_OUT
                assignment.timed_out = True
                assignment.timed_out_at = now
                assignment.processing_end_at = now
                assignment.save()
                
                # 2. Update page status back to PENDING so it gets reassigned
                assignment.page.status = PageStatus.PENDING
                assignment.page.save()
                
                # 3. Update Resource stats
                prof = assignment.resource
                prof.rejection_count = F('rejection_count') + 1
                prof.save(update_fields=['rejection_count'])
                prof.refresh_status()
                
                # 4. Notify via WS
                from apps.processing.consumers import send_timeout_notification
                send_timeout_notification(assignment.resource.user.id, assignment.page.page_number)
                
                # 5. Audit Log for Timeout
                AuditLog.objects.create(
                    action=AuditEventType.TIMEOUT, # Correct enum
                    assignment_id=assignment.id,
                    document_id=assignment.document.id,
                    actor=None,
                    old_status=PageAssignmentStatus.IN_PROGRESS,
                    new_status=PageAssignmentStatus.TIMED_OUT,
                    metadata={'reason': 'SLA Expired', 'page_number': assignment.page.page_number}
                )
                
            timeouts_triggered += 1

        # 8 minute warning (480s)
        elif elapsed >= (assignment.max_processing_time * 0.8) and not assignment.time_warning_sent:
            assignment.time_warning_sent = True
            assignment.save(update_fields=['time_warning_sent'])
            
            from apps.processing.consumers import send_time_warning
            send_time_warning(
                assignment.resource.user.id, 
                assignment.page.page_number, 
                assignment.max_processing_time - elapsed
            )
            warnings_sent += 1

    if timeouts_triggered > 0 or warnings_sent > 0:
        logger.info(f"Timeouts: {timeouts_triggered} revoked, {warnings_sent} warnings sent.")
    
    # If any timed out, trigger the assignment engine to pick up the dropped pages
    if timeouts_triggered > 0:
        assign_pages_task.delay()


@shared_task(bind=True, max_retries=3)
def process_page_ocr_task(self, page_id):
    """
    Runs OCR/Extraction via OCRService and then triggers validation.
    """
    from apps.processing.services.ocr import OCRService
    try:
        page = Page.objects.get(id=page_id)
        OCRService.process_page(page)
        # After OCR, trigger positional extraction
        extract_page_blocks_task.delay(page_id)
    except Page.DoesNotExist:
        logger.warning(f"Page {page_id} not found for OCR.")
    except Exception as exc:
        logger.error(f"OCR failed for page {page_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)

@shared_task(bind=True, max_retries=3)
def extract_page_blocks_task(self, page_id):
    """
    Extracts positional blocks and tables using PDFLayoutEngine.
    """
    from apps.documents.models import Page, Block, PageTable
    from apps.processing.services.layout_engine import PDFLayoutEngine
    
    try:
        page = Page.objects.get(id=page_id)
        if not page.content_file:
            return
            
        engine = PDFLayoutEngine()
        layout = engine.extract_page_layout(page.content_file.path, page_index=0)
        
        with transaction.atomic():
            # Clear existing
            Block.objects.filter(page=page).delete()
            PageTable.objects.filter(page=page).delete()
            
            # Save blocks
            blocks_to_create = []
            for idx, blk in enumerate(layout['blocks']):
                blocks_to_create.append(Block(
                    page=page,
                    block_index=idx,
                    block_id=blk['block_id'],
                    block_type=blk['block_type'],
                    extracted_text=blk['text'],
                    original_text=blk['text'],  # Keep as source of truth
                    current_text=blk['text'],
                    x=blk['x'],
                    y=blk['y'],
                    width=blk['width'],
                    height=blk['height'],
                    font_name=blk['font_family'],
                    font_size=blk['font_size'],
                    font_weight=blk['font_weight'],
                    font_style=blk['font_style'],
                    font_color=blk['color'],
                    table_id=blk.get('table_id') or '',
                    row_index=blk.get('row_index'),
                    col_index=blk.get('col_index')
                ))
            Block.objects.bulk_create(blocks_to_create)
            
            # Save tables
            for tbl in layout['tables']:
                PageTable.objects.create(
                    page=page,
                    table_ref=tbl['table_id'],
                    x=tbl['x'],
                    y=tbl['y'],
                    width=tbl['width'],
                    height=tbl['height'],
                    row_count=tbl['row_count'],
                    col_count=tbl['col_count'],
                    table_json=tbl['rows']
                )
            
            # Update page
            page.pdf_page_width = layout['page_width']
            page.pdf_page_height = layout['page_height']
            page.blocks_extracted = True
            page.blocks_count = len(blocks_to_create)
            page.has_tables = layout['has_tables']
            page.save()
            
        # Continue to validation
        validate_single_page.delay(page_id)
        
    except Page.DoesNotExist:
        logger.warning(f"Page {page_id} not found for extraction.")
    except Exception as exc:
        logger.error(f"Layout extraction failed for page {page_id}: {exc}")
        raise self.retry(exc=exc, countdown=60)

# ── Section 4 & 5: Validation and Complexity ───────────────
@shared_task(bind=True, max_retries=3)
def validate_single_page(self, page_id):
    """
    Validates a single page.
    """
    try:
        page = Page.objects.get(id=page_id)
    except Page.DoesNotExist:
        return

    # 1. Validate
    validator = PageValidator()
    result = validator.validate_page(page)
    
    if result.passed:
        page.validation_status = ValidationStatus.VALIDATED
        page.status = PageStatus.PENDING # Ready for assignment
    else:
        page.validation_status = ValidationStatus.VALIDATION_FAILED
        page.status = PageStatus.FAILED
        page.validation_errors = [c['message'] for c in result.checks if not c['passed']]
        
    page.save(update_fields=['validation_status', 'status', 'validation_errors'])
    
    # Trigger scoring independently (could be chained, but we want it done regardless)
    score_page_complexity.delay(page_id)

@shared_task(bind=True, max_retries=5)
def score_page_complexity(self, page_id):
    """
    Scores the complexity of a page.
    """
    try:
        with transaction.atomic():
            page = Page.objects.select_for_update().get(id=page_id)
            ComplexityScorer.apply_score(page)
    except Page.DoesNotExist:
        return
    except Exception as exc:
        logger.error(f"Scoring failed for page {page_id}: {exc}")
        # Apply safe default on ultimate failure
        if self.request.retries >= self.max_retries:
            Page.objects.filter(id=page_id).update(
                complexity_type=ComplexityType.SIMPLE,
                complexity_weight=1.0,
                complexity_scored_at=timezone.now()
            )
        raise self.retry(exc=exc, countdown=30)

@shared_task
def mark_document_ready_to_assign(document_id):
    """
    Final check before document hits the queue.
    Ensures ALL pages have weights.
    """
    from common.enums import PipelineStatus, DocumentStatus
    
    with transaction.atomic():
        doc = Document.objects.select_for_update().get(id=document_id)
        pages = doc.pages.all()
        
        # Guard: Ensure all pages have a weight
        missing_weights = pages.filter(complexity_weight__isnull=True)
        if missing_weights.exists():
            missing_weights.update(complexity_weight=1.0)
            
        doc.pipeline_status = PipelineStatus.READY_TO_ASSIGN
        doc.status = DocumentStatus.READY
        doc.save(update_fields=['pipeline_status', 'status'])
        
        # ✅ Add to Queue (Crucial for assignment engine)
        from apps.processing.models import DocumentQueue
        from common.enums import QueueStatus
        DocumentQueue.objects.get_or_create(
            document=doc,
            defaults={'status': QueueStatus.WAITING}
        )
        
        # Trigger assignment
        from apps.processing.tasks import assign_pages_task
        assign_pages_task.delay()


@shared_task
def validate_all_pages(document_id):
    try:
        doc = Document.objects.get(id=document_id)
        doc.pipeline_status = PipelineStatus.VALIDATING
        doc.save(update_fields=['pipeline_status'])
        
        pages = doc.pages.all()
        for page in pages:
            validate_single_page.delay(page.id)
            
    except Document.DoesNotExist:
        pass


# ── Section 8: File Conversion (Word to PDF) ───────────────
@shared_task(bind=True, max_retries=3)
def convert_word_to_pdf(self, document_id):
    import subprocess
    from django.core.files import File
    from django.core.files.temp import NamedTemporaryFile
    
    try:
        doc = Document.objects.get(id=document_id)
        doc.pipeline_status = PipelineStatus.CONVERTING
        doc.save(update_fields=['pipeline_status'])
        
        # Skip if already PDF
        if doc.file.name.lower().endswith('.pdf'):
            doc.pipeline_status = PipelineStatus.SPLITTING
            doc.save(update_fields=['pipeline_status'])
            # Trigger split
            from apps.documents.tasks import process_document_task
            process_document_task.delay(doc.id)
            return
            
        # Very basic mock logic for LibreOffice conversion on server
        # In actual prod, this hits unoconv or a microservice
        # ...
        
        # For now, just mark it ready to split to keep pipeline flowing
        # (Assuming the system ensures file uploaded is PDF for this iteration)
        doc.pipeline_status = PipelineStatus.SPLITTING
        doc.save(update_fields=['pipeline_status'])
        from apps.documents.tasks import process_document_task
        process_document_task.delay(doc.id)
        
    except Exception as exc:
        doc.pipeline_status = PipelineStatus.FAILED
        doc.pipeline_error = f"Conversion failed: {str(exc)}"
        doc.save()
        raise self.retry(exc=exc, countdown=60)


# ── Section 10: Merge Document ─────────────────────────────
@shared_task(bind=True, max_retries=5)
def merge_document_pages(self, document_id, admin_user_id=None):
    from apps.processing.services.merge import MergeService
    try:
        doc = Document.objects.get(id=document_id)
        doc.pipeline_status = PipelineStatus.MERGING
        doc.save(update_fields=['pipeline_status'])
        
        MergeService.merge_approved_pages(doc, admin_user_id)
        
        doc.pipeline_status = PipelineStatus.MERGED
        doc.save(update_fields=['pipeline_status'])
        
        # Notify Admin WS
        from apps.processing.consumers import broadcast_admin_update
        broadcast_admin_update({
            'type': 'document_merged',
            'doc_ref': doc.doc_ref,
            'title': doc.title
        })
        
    except Exception as exc:
        countdown = (2 ** self.request.retries) * 60
        raise self.retry(exc=exc, countdown=countdown)


# ── Existing Task Wrapped for New Spec ──────────────────────
@shared_task
def assign_pages_task():
    """
    Distributed assignment trigger.
    Processes the queue until empty or resources saturated.
    """
    total = 0
    # Safety break to prevent infinite loops in a single task
    for _ in range(100):
        assigned = AssignmentService.assign_pages()
        if assigned == 0:
            break
        total += assigned
    return f"Assigned {total} pages total"

@shared_task
def resplit_missing_pages(document_id):
    """
    Identifies and re-splits only missing pages from the source PDF.
    """
    import fitz
    import os
    from apps.documents.models import Document, Page
    from common.enums import PageStatus
    from django.core.files import File
    from pathlib import Path
    from django.conf import settings
    from django.utils.timezone import now
    
    with transaction.atomic():
        doc = Document.objects.select_for_update().get(id=document_id)
        
        # Identify missing or broken pages
        db_pages = Page.objects.filter(document=doc).values_list('page_number', flat=True)
        missing_nums = [i for i in range(1, (doc.total_pages or 0) + 1) if i not in db_pages]
        
        for p in Page.objects.filter(document=doc):
             if not p.content_file or not os.path.exists(p.content_file.path):
                 missing_nums.append(p.page_number)
        
        if not missing_nums:
            return "No missing pages found."
            
        missing_nums = sorted(list(set(missing_nums)))
        
        pdf_file = doc.file if doc.file else doc.original_file
        pdf = fitz.open(pdf_file.path)
        
        new_page_ids = []
        for num in missing_nums:
            idx = num - 1
            if idx < 0 or idx >= pdf.page_count: continue
            
            single_pdf = fitz.open()
            single_pdf.insert_pdf(pdf, from_page=idx, to_page=idx)
            
            out_dir = Path(settings.MEDIA_ROOT) / 'pages' / 'splits' / now().strftime('%Y/%m/%d') / str(doc.id)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f'page_{num:04d}_repair.pdf'
            
            single_pdf.save(str(out_path))
            single_pdf.close()
            
            with open(out_path, 'rb') as f:
                django_file = File(f, name=out_path.name)
                page, _ = Page.objects.update_or_create(
                    document=doc,
                    page_number=num,
                    defaults={'content_file': django_file, 'status': PageStatus.PENDING}
                )
                new_page_ids.append(page.id)
        
        pdf.close()
        
        # Restart pipeline for fixed pages
        for pid in new_page_ids:
            # Chain: OCR -> Extract -> Validate
            extract_page_blocks_task.delay(pid)
            
        return f"Repaired {len(new_page_ids)} pages for document {document_id}"
