import logging
import json
import itertools
from decimal import Decimal
from django.db import models
from django.db import transaction
from django.db.models import Count, Q, F, Sum, FloatField
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.core.files.base import ContentFile

from apps.accounts.models import ResourceProfile
from apps.documents.models import Document, Page
from apps.processing.models import PageAssignment
from apps.audit.models import AuditLog
from common.enums import (
    PageStatus, PageAssignmentStatus, DocumentStatus, 
    AssignmentStatus, PipelineStatus, AuditEventType, UserRole
)

logger = logging.getLogger(__name__)

class AssignmentService:
    TARGET_BLOCK_WEIGHT = 10.0 # Max complexity weight per assignment cycle
    
    @staticmethod
    def get_available_resources():
        """
        Returns resources that can accept new work.
        Always queries DB fresh — never uses cached queryset.
        Annotates with computed current_load for ordering.
        """
        from common.enums import ResourceStatus, PageAssignmentStatus
        
        return ResourceProfile.objects.filter(
            status=ResourceStatus.ACTIVE,
            is_available=True,
        ).annotate(
            # Compute load directly in SQL — no drift possible
            computed_load=Coalesce(
                Sum(
                    'page_assignments__page__complexity_weight',
                    filter=Q(
                        page_assignments__status__in=[
                            PageAssignmentStatus.ASSIGNED, 
                            PageAssignmentStatus.IN_PROGRESS
                        ]
                    )
                ),
                0.0,
                output_field=FloatField()
            )
        ).filter(
            # Only resources where computed_load < max_capacity
            computed_load__lt=F('max_capacity')
        ).order_by('-last_active_at', 'computed_load') # Prioritize recently active users

    @staticmethod
    def calculate_pages_for_resource(resource, available_pages, current_load=None):
        """
        Calculate how many pages this resource can accept
        based on their remaining capacity and page weights.
        If 'current_load' is provided, uses it (useful for batch cycles).
        Otherwise always re-fetches current load from DB (not cached).
        """
        if current_load is None:
            # ✅ Only fetch if not provided (fallback)
            resource.refresh_from_db()
            current_load = resource.current_load

        remaining_capacity = max(0.0, float(resource.max_capacity) - current_load)
        if remaining_capacity <= 0:
            return []

        allocated    = []
        total_weight = 0.0

        for page in available_pages:
            # ✅ Guard against NULL weight
            page_weight = page.complexity_weight
            if page_weight is None or page_weight <= 0:
                page_weight = 1.0  # safe default
                # Also fix in DB
                Page.objects.filter(pk=page.pk).update(
                    complexity_weight=1.0
                )

            if total_weight + page_weight <= min(
                AssignmentService.TARGET_BLOCK_WEIGHT,
                remaining_capacity
            ):
                allocated.append(page)
                total_weight += page_weight
            else:
                break

        return allocated

    @staticmethod
    def _broadcast_resource_update(resource):
        """
        Helper to broadcast resource status via WebSockets.
        """
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            channel_layer = get_channel_layer()
            if channel_layer:
                async_to_sync(channel_layer.group_send)(
                    'admin_notifications',
                    {
                        'type': 'resource_status_update',
                        'payload': {
                            'resource_id':   resource.pk,
                            'username':      resource.user.username,
                            'status':        resource.status,
                            'current_load':  round(resource.current_load, 2),
                            'max_capacity':  resource.max_capacity,
                            'remaining':     round(resource.remaining_capacity, 2),
                        }
                    }
                )
        except: pass

    @staticmethod
    @transaction.atomic
    def assign_pages_to_resource(page_ids, resource, broadcast=True):
        """
        Atomically assign pages to a resource.
        Validates resource status and capacity INSIDE transaction
        to prevent race conditions.
        """
        from rest_framework.exceptions import ValidationError
        from common.enums import ResourceStatus, PageStatus, PageAssignmentStatus, AuditEventType

        # Lock resource row to prevent concurrent over-assignment
        resource = ResourceProfile.objects.select_for_update().get(
            pk=resource.pk
        )

        # Re-validate INSIDE transaction
        if resource.status != ResourceStatus.ACTIVE:
            raise ValidationError({
                'error':   True,
                'code':    'RESOURCE_INACTIVE',
                'message': f'Resource {resource.user.username} '
                           f'is {resource.status}',
            })

        current_load = resource.current_load
        pages_to_assign = Page.objects.filter(
            pk__in=page_ids
        ).select_for_update()

        # Validate total weight fits in remaining capacity
        total_new_weight = sum(
            (p.complexity_weight or 1.0) for p in pages_to_assign
        )

        if current_load + total_new_weight > resource.max_capacity:
            raise ValidationError({
                'error':   True,
                'code':    'RESOURCE_AT_CAPACITY',
                'message': f'Assignment would exceed capacity. '
                           f'Remaining: {resource.remaining_capacity:.1f}, '
                           f'Requested weight: {total_new_weight:.1f}',
            })

        assignments = []
        for page in pages_to_assign:
            # Validate page is not already assigned
            if page.status in [PageStatus.ASSIGNED, PageStatus.IN_PROGRESS]:
                continue  # skip already-assigned pages silently

            assignment = PageAssignment.objects.create(
                page=page,
                resource=resource,
                document=page.document,
                status=PageAssignmentStatus.ASSIGNED,
                max_processing_time=int(600 * (page.complexity_weight or 1.0)),
            )
            page.status = PageStatus.ASSIGNED
            page.current_assignee = resource.user
            page.is_locked = True
            page.save(update_fields=['status', 'current_assignee', 'is_locked'])
            
            # Audit
            AuditLog.objects.create(
                action=AuditEventType.ASSIGNED,
                assignment_id=assignment.id,
                document_id=page.document.id,
                actor=None,
                metadata={
                    'resource_id': resource.id, 
                    'page_number': page.page_number,
                    'is_reassignment': False
                }
            )
            assignments.append(assignment)

        # Refresh status after assignment
        resource.refresh_status()
        
        if broadcast:
            AssignmentService._broadcast_resource_update(resource)

        return assignments

    @staticmethod
    def assign_pages(document_id=None):
        """
        Main entry point for the assignment engine.
        Allocates pending pages to available resources.
        """
        from apps.processing.models import DocumentQueue
        from common.enums import QueueStatus, PageStatus, PipelineStatus

        with transaction.atomic():
            # 1. Get candidate documents from queue
            query = DocumentQueue.objects.select_for_update(skip_locked=True)
            if document_id:
                queue_items = query.filter(document_id=document_id)
            else:
                queue_items = query.filter(status=QueueStatus.WAITING).order_by('position', 'created_at')

            total_assigned = 0
            
            for item in queue_items:
                doc = item.document
                
                # Fetch pending pages
                pending_pages = Page.objects.filter(
                    document=doc,
                    status__in=[PageStatus.PENDING, PageStatus.FAILED]
                ).order_by('page_number')
                
                if not pending_pages.exists():
                    item.status = QueueStatus.ASSIGNED
                    item.save(update_fields=['status'])
                    continue

            # 3. Get all resources once at the start of the cycle
            resources = list(AssignmentService.get_available_resources())
            if not resources:
                logger.info("No active resources available for assignment.")
                return 0

            # Maintain in-memory load mapping to prevent over-assignment during same transaction
            resource_loads = {r.id: r.current_load for r in resources}
            updated_resources = set()

            for item in queue_items:
                doc = item.document
                
                # Fetch pending pages
                pending_pages = Page.objects.filter(
                    document=doc,
                    status__in=[PageStatus.PENDING, PageStatus.FAILED]
                ).order_by('page_number')
                
                if not pending_pages.exists():
                    logger.info(f"Document {doc.id} ({doc.title}) has no pending pages. Marking queue ASSIGNED.")
                    item.status = QueueStatus.ASSIGNED
                    item.save(update_fields=['status'])
                    continue

                # Attempt to assign pages to each resource
                pages_list = list(pending_pages)
                page_ptr = 0
                
                logger.info(f"Processing Document {doc.id} ({doc.title}) with {len(pages_list)} pending pages.")
                
                for res in resources:
                    if page_ptr >= len(pages_list):
                        break
                    
                    # Calculate how many pages this resource can take using IN-MEMORY load
                    to_assign = AssignmentService.calculate_pages_for_resource(
                        res, pages_list[page_ptr:], current_load=resource_loads.get(res.id)
                    )
                    
                    if to_assign:
                        page_ids = [p.pk for p in to_assign]
                        try:
                            # Use a sub-transaction point for each assignment attempt so one failure doesn't roll back the whole task run
                            with transaction.atomic():
                                AssignmentService.assign_pages_to_resource(page_ids, res, broadcast=False)
                                
                                # Update in-memory load for next doc/page
                                block_weight = sum(float(p.complexity_weight or 1.0) for p in to_assign)
                                resource_loads[res.id] += block_weight
                                
                                page_ptr += len(to_assign)
                                total_assigned += len(to_assign)
                                updated_resources.add(res)
                                
                                logger.info(f"Assigned {len(page_ids)} pages of {doc.title} to {res.user.username}. New load: {resource_loads[res.id]}")
                        except Exception as e:
                            logger.error(f"Failed to assign pages to {res.user.username}: {str(e)}")
                            # Continue to next resource or doc
                
                # If all pages of this doc were assigned, mark queue item complete
                if page_ptr >= len(pages_list):
                    item.status = QueueStatus.ASSIGNED
                    item.save(update_fields=['status'])
                    doc.pipeline_status = PipelineStatus.IN_PROGRESS
                    doc.save(update_fields=['pipeline_status'])
                    logger.info(f"Document {doc.title} fully assigned and moved to IN_PROGRESS.")
                else:
                    logger.info(f"Document {doc.title} partially assigned. {len(pages_list) - page_ptr} pages remaining.")
            
            # Bulk broadcast for all updated resources
            for res in updated_resources:
                AssignmentService._broadcast_resource_update(res)

            if total_assigned > 0:
                logger.info(f"Assignment cycle completed. Total assigned: {total_assigned}")
                
            return total_assigned


    @staticmethod
    def reassign_rejected_assignment(assignment_id, manager_user, auto_assign=True):
        """
        Reassigns a REJECTED PageAssignment (Section 9).
        """
        from common.enums import RejectionReason
        
        with transaction.atomic():
            old_assignment = PageAssignment.objects.select_for_update().get(id=assignment_id)
            doc = old_assignment.document
            page = old_assignment.page
            
            # 1. Reset Page & Cancel Old Assignment
            old_assignment.status = PageAssignmentStatus.REASSIGNED
            old_assignment.save()

            page.status = PageStatus.PENDING
            page.current_assignee = None
            page.save()

            if not auto_assign:
                return None

            # 2. Get active candidates EXCLUDING ALL previous failed resources for this page
            from common.enums import ResourceStatus
            from django.core.cache import cache
            from apps.processing.models import RejectedPage
            
            # Use global PageAssignment for query
            excluded_resource_ids = set(PageAssignment.objects.filter(
                page=page,
                status__in=[PageAssignmentStatus.REASSIGNED, PageAssignmentStatus.TIMED_OUT]
            ).values_list('resource_id', flat=True))
            
            # Also check RejectedPage exclusions (manually marked by admin)
            page_rejection_exclusions = RejectedPage.objects.filter(page=page).values_list('excluded_resources__id', flat=True)
            excluded_resource_ids.update(filter(None, page_rejection_exclusions))
            
            # Ensure the current resource being rejected is definitely excluded
            if old_assignment.resource_id:
                excluded_resource_ids.add(old_assignment.resource_id)
            
            candidates = list(ResourceProfile.objects.filter(
                status=ResourceStatus.ACTIVE,
                is_available=True,
                user__is_active=True
            ).exclude(id__in=list(excluded_resource_ids)).select_related('user'))
            
            logger.info(f"Reassigning Page {page.page_number}. Excluded IDs: {excluded_resource_ids}. Candidate count: {len(candidates)}")

            best_candidate = None
            max_score = -float('inf')

            for cand in candidates:
                # Redis-based presence priority
                is_online = cache.get(f"user:{cand.user.id}:online") == "true"
                presence_bonus = 10000 if is_online else 0
                
                cap = cand.max_capacity - cand.current_load
                if cap > 0:
                    score = (cap * 2) - (cand.avg_processing_time * 0.5) - (cand.rejection_count * 3) + presence_bonus
                    if score > max_score:
                        max_score = score
                        best_candidate = cand
            
            # If still no candidate (e.g. all at full capacity), pick ANY available resource regardless of capacity
            if not best_candidate:
                for cand in candidates:
                    best_candidate = cand
                    break

            if not best_candidate:
                # Add back into queue for assign_pages task to pick up later
                return None

            # 3. Create new assignment
            new_assign = PageAssignment.objects.create(
                page=page,
                resource=best_candidate,
                document=doc,
                status=PageAssignmentStatus.ASSIGNED,
                is_reassigned=True,
                reassignment_count=old_assignment.reassignment_count + 1,
                reassigned_from=old_assignment,
                max_processing_time=int(600 * getattr(page, 'complexity_weight', 1.0))
            )
            
            # Update page back to ASSIGNED with new user
            page.status = PageStatus.ASSIGNED
            page.current_assignee = best_candidate.user
            page.save()
            
            # 4. Create ReassignmentLog audit trail
            from apps.processing.models import ReassignmentLog
            ReassignmentLog.objects.create(
                original_assignment=old_assignment,
                new_assignment=new_assign,
                reassigned_by=manager_user,
                reason=RejectionReason.QUALITY_FAIL, # Defaults
                previous_resource=old_assignment.resource,
                new_resource=best_candidate
            )
            
            return new_assign


class ProcessingService:

    @staticmethod
    def complete_assignment(assignment_id, resource_user, uploaded_file=None):
        """
        Marks a PageAssignment as SUBMITTED (Section 4).
        Creates a SubmittedPage record for Admin Review.
        """
        from apps.processing.models import SubmittedPage
        from common.enums import ReviewStatus
        
        with transaction.atomic():
            # 1. Lock and Validate
            try:
                assignment = PageAssignment.objects.select_for_update().get(id=assignment_id)
            except PageAssignment.DoesNotExist:
                raise ValueError("Assignment not found")

            is_admin = resource_user.role == UserRole.ADMIN or resource_user.is_superuser or resource_user.is_staff
            if assignment.resource.user != resource_user and not is_admin:
                raise PermissionError("User is not the assignee of this page")
            
            if assignment.status not in [PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]:
                 raise ValueError("Assignment is not ACTIVE")

            page = assignment.page
            
            # 2. Update Page Status (HTML Text is already saved via WebSocket)
            now = timezone.now()
            page.status = PageStatus.SUBMITTED
            page.current_assignee = None
            page.is_locked = False
            page.processing_completed_at = now
            page.processing_end_date = now.date()
            page.processing_end_time = now.time()
            if not page.processing_started_at:
                page.processing_started_at = now
                page.processing_start_date = now.date()
                page.processing_start_time = now.time()
            page.save()

            # 3. Update Assignment Status
            assignment.status = PageAssignmentStatus.SUBMITTED
            assignment.submitted_at = now
            assignment.processing_end_at = now
            if not assignment.processing_start_at:
                assignment.processing_start_at = now
            assignment.save()
            
            # 4. Record SubmittedPage for Review
            submission, created = SubmittedPage.objects.update_or_create(
                assignment=assignment,
                defaults={
                    'page': page,
                    'document': assignment.document,
                    'submitted_by': resource_user,
                    'page_number': page.page_number,
                    'final_text': page.text_content or "",
                    'processing_duration': assignment.processing_duration,
                    'processing_start_at': assignment.processing_start_at,
                    'processing_end_at': assignment.processing_end_at,
                    'review_status': ReviewStatus.PENDING_REVIEW
                }
            )

            try:
                import fitz
                from io import BytesIO
                import os
                
                if assignment.document.file and os.path.exists(assignment.document.file.path):
                    # Generate snapshot PDF of this single page for admin review
                    doc = fitz.open(assignment.document.file.path)
                    pdf_page_idx = page.page_number - 1
                    if 0 <= pdf_page_idx < len(doc):
                        pdf_page = doc[pdf_page_idx]
                        
                        if page.text_content:
                            from bs4 import BeautifulSoup
                            soup = BeautifulSoup(page.text_content, 'html.parser')
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
                            
                            # Pass 2: Insert
                            for el in elements:
                                try:
                                    bbox = json.loads(el['data-bbox'].replace('(', '[').replace(')', ']'))
                                    if not bbox: continue
                                    text_val = el.get_text().strip()
                                    if text_val:
                                        pdf_page.insert_textbox(
                                            fitz.Rect(bbox), 
                                            text_val,
                                            fontsize=10,
                                            fontname="helv",
                                            align=0
                                        )
                                except: continue
                        
                        result_buffer = BytesIO()
                        
                        # Create a new 1-page PDF
                        single_page_doc = fitz.open()
                        single_page_doc.insert_pdf(doc, from_page=pdf_page_idx, to_page=pdf_page_idx)
                        single_page_doc.save(result_buffer)
                        single_page_doc.close()
                        doc.close()
                        
                        filename = f"submitted_page_{page.page_number}_{assignment.id}.pdf"
                        submission.output_page_file.save(filename, ContentFile(result_buffer.getvalue()), save=True)
            except Exception as pdf_err:
                logger.error(f"Error generating submitted page PDF snapshot: {pdf_err}")

                
            # 5. Audit Log
            AuditLog.objects.create(
                action=AuditEventType.COMPLETED,
                assignment_id=assignment.id,
                document_id=assignment.document.id,
                actor=resource_user,
                new_status=AssignmentStatus.COMPLETED, # Meta enum
                metadata={
                    'duration_sec': assignment.processing_duration.total_seconds() if assignment.processing_duration else 0
                }
            )

            # Signal handler check_all_pages_submitted triggers automatically on SubmittedPage save
            # Resource current_load drops automatically via PageAssignment status change signal
            
            # Post-commit schedule assign pages
            from apps.processing.tasks import assign_pages_task
            transaction.on_commit(lambda: assign_pages_task.delay())

