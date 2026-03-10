from rest_framework import viewsets, permissions, status, views
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action
import logging
from django.utils import timezone
from django.shortcuts import get_object_or_404
from django.db import transaction

from apps.documents.models import Document, Page
from apps.accounts.models import ResourceProfile
from apps.processing.models import PageAssignment, SubmittedPage, ReassignmentLog
from apps.processing.serializers import (
    PageAssignmentSerializer, SubmittedPageSerializer, 
    StartProcessingSerializer, SubmitProcessingSerializer
)
from apps.documents.serializers import BlockSerializer, PageTableSerializer
from apps.processing.services.core import AssignmentService, ProcessingService
from common.enums import (
    PageAssignmentStatus, UserRole, ResourceStatus, 
    PipelineStatus, ReviewStatus, PageStatus
)
from rest_framework.decorators import api_view, permission_classes

logger = logging.getLogger(__name__)

class WorkspaceViewSet(viewsets.ViewSet):
    """
    Section 11 APIs for the Resource Workspace.
    Using `doc_ref` and `page_number` directly instead of DB IDs.
    """
    permission_classes = [permissions.IsAuthenticated]
    
    def _get_assignment(self, doc_ref, page_number, user):
        """Helper to fetch the relevant assignment for this user, including non-active ones."""
        is_admin = user.role == UserRole.ADMIN or user.is_superuser or user.is_staff
        
        # Allow both admins AND resources to see SUBMITTED/APPROVED if it's theirs
        status_filter = [
            PageAssignmentStatus.ASSIGNED, 
            PageAssignmentStatus.IN_PROGRESS,
            PageAssignmentStatus.SUBMITTED,
            PageAssignmentStatus.APPROVED
        ]

        try:
            filters = {
                'document__doc_ref': doc_ref,
                'page__page_number': page_number,
                'status__in': status_filter
            }
            if not is_admin:
                filters['resource__user'] = user
                
            assignment = PageAssignment.objects.filter(
                **filters
            ).select_related('document', 'page', 'resource__user').latest('assigned_at')
            
            if assignment.resource.user != user and not is_admin:
                raise PermissionError("Access denied")
                
            return assignment
        except PageAssignment.DoesNotExist:
            if is_admin:
                page = get_object_or_404(Page, document__doc_ref=doc_ref, page_number=page_number)
                return PageAssignment(document=page.document, page=page, status=PageAssignmentStatus.IN_PROGRESS)
            raise
            
    def _get_or_seed_blocks(self, page):
        """
        No longer seeds on the fly; extraction is pre-computed by Celery.
        This just returns the pre-extracted blocks.
        """
        from apps.documents.models import Block
        blocks = page.blocks.all().order_by('y', 'x')
        if not blocks.exists() and page.blocks_extracted:
            # If for some reason extraction flag is set but blocks are missing,
            # it might be an extraction failure or race condition.
            logger.warning(f"Page {page.id} marked as extracted but has no blocks.")
        return blocks
    
    def list(self, request):
        """List active assignments for the current resource, grouped by document."""
        assignments = PageAssignment.objects.filter(
            resource__user=request.user,
            status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
        ).select_related('document', 'page').order_by('document_id', 'page__page_number')
        
        grouped_data = {}
        for a in assignments:
            doc_id = a.document_id
            if doc_id not in grouped_data:
                grouped_data[doc_id] = {
                    'document_ref': a.document.doc_ref,
                    'document_title': a.document.title,
                    'document_total_pages': a.document.total_pages,
                    'pages': [],
                    'status': a.status,
                    'assigned_at': a.assigned_at
                }
            grouped_data[doc_id]['pages'].append(a.page.page_number)
            if a.status == PageAssignmentStatus.IN_PROGRESS:
                grouped_data[doc_id]['status'] = PageAssignmentStatus.IN_PROGRESS

        results = []
        for doc_id, data in grouped_data.items():
            pages = sorted(data['pages'])
            page_range = f"Pages {pages[0]}-{pages[-1]}" if len(pages) > 1 else f"Page {pages[0]}"
            
            results.append({
                'id': doc_id,
                'document_ref': data['document_ref'],
                'document_title': data['document_title'],
                'document_total_pages': data['document_total_pages'],
                'page_number': pages[0],
                'page_range': page_range,
                'total_pages': len(pages),
                'status': data['status'],
                'status_display': data['status'].replace('_', ' ').title(),
                'assigned_at': data['assigned_at']
            })

        return Response(results)
    
    @action(detail=False, methods=['get'])
    def history(self, request):
        """List submitted pages for the current resource"""
        submissions = SubmittedPage.objects.filter(
            submitted_by=request.user
        ).select_related('document', 'page').order_by('-submitted_at')
        
        serializer = SubmittedPageSerializer(submissions, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='content') # Keep for router listing
    def get_workspace_data(self, request, doc_ref=None, page_number=None):
        try:
            is_admin = request.user.role == UserRole.ADMIN or request.user.is_superuser or request.user.is_staff
            target_assignment = self._get_assignment(doc_ref, page_number, request.user)
            
            # Logic for "Bundle" Visibility
            if is_admin:
                # Admins see ALL pages of the document for full audit context
                all_assignments = PageAssignment.objects.filter(
                    document__doc_ref=doc_ref
                ).select_related('page').order_by('page__page_number')
                
                # If a page has NO assignment but exists, we still want to show it in the admin block?
                # For now, let's just show all pages that have ever been assigned or are currently in the document
                pages_data_map = {}
                from apps.processing.services.nlp_engine import NLPInspector
                
                # 1. Start with all pages in document
                all_pages = Page.objects.filter(document__doc_ref=doc_ref).order_by('page_number')
                for p in all_pages:
                    suggestions = []
                    try:
                        suggestions = NLPInspector.analyze_page_structure(p)
                    except Exception as nlp_err:
                        logger.warning(f"NLP Analysis failed for page {p.id}: {nlp_err}")

                    blocks = self._get_or_seed_blocks(p)
                    blocks_data = BlockSerializer(blocks, many=True).data
                    
                    tables = p.tables.all()
                    tables_data = PageTableSerializer(tables, many=True).data
                    
                    pages_data_map[p.page_number] = {
                        'id': p.id,
                        'page_number': p.page_number,
                        'text_content': p.text_content,
                        'layout_data': p.layout_data,
                        'blocks': blocks_data,
                        'tables': tables_data,
                        'suggestions': suggestions,
                        'image_url': request.build_absolute_uri(p.content_file.url) if p.content_file else None,
                        'assignment_status': 'UNASSIGNED',
                        'is_readonly': False # Admins can edited unassigned pages if they want
                    }
                
                # 2. Layer on assignment data
                for a in all_assignments:
                    if a.page.page_number in pages_data_map:
                        # For admins, we only force readonly if they are viewing a page specifically assigned to someone ELSE and that person has submitted?
                        # Actually, let's just let admins always edit for now.
                        asgn_readonly = False 
                        if not is_admin:
                            asgn_readonly = a.status not in [PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
                            
                        pages_data_map[a.page.page_number].update({
                            'assignment_id': a.id,
                            'assignment_status': a.status,
                            'is_readonly': asgn_readonly
                        })
                
                pages_data = [v for k, v in sorted(pages_data_map.items())]
            else:
                # Resources see their current active block PLUS their historical submitted pages for this doc
                all_user_assignments = PageAssignment.objects.filter(
                    document__doc_ref=doc_ref,
                    resource__user=request.user,
                    status__in=[
                        PageAssignmentStatus.ASSIGNED, 
                        PageAssignmentStatus.IN_PROGRESS, 
                        PageAssignmentStatus.SUBMITTED,
                        PageAssignmentStatus.APPROVED
                    ]
                ).select_related('page').order_by('page__page_number')
                
                pages_data = []
                from apps.processing.services.nlp_engine import NLPInspector
                
                for assignment in all_user_assignments:
                    suggestions = []
                    try:
                        suggestions = NLPInspector.analyze_page_structure(assignment.page)
                    except Exception as nlp_err:
                        logger.warning(f"NLP Analysis failed for assignment {assignment.id}: {nlp_err}")
                        
                    blocks = self._get_or_seed_blocks(assignment.page)
                    blocks_data = BlockSerializer(blocks, many=True).data
                    
                    tables = assignment.page.tables.all()
                    tables_data = PageTableSerializer(tables, many=True).data

                    pages_data.append({
                        'id': assignment.page.id,
                        'page_number': assignment.page.page_number,
                        'text_content': assignment.page.text_content,
                        'layout_data': assignment.page.layout_data,
                        'blocks': blocks_data,
                        'tables': tables_data,
                        'suggestions': suggestions,
                        'image_url': request.build_absolute_uri(assignment.page.content_file.url) if assignment.page.content_file else None,
                        'assignment_id': assignment.id,
                        'assignment_status': assignment.status,
                        'is_readonly': assignment.status not in [PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
                    })

            return Response({
                'document': {
                    'name': target_assignment.document.name,
                    'doc_ref': doc_ref,
                    'total_pages': target_assignment.document.total_pages,
                    'document_url': request.build_absolute_uri(target_assignment.document.file.url) if target_assignment.document.file else None
                },
                'pages': pages_data,
                'is_block': len(pages_data) > 1,
                'view_mode': 'ADMIN' if is_admin else 'RESOURCE'
            })
            
        except PageAssignment.DoesNotExist:
            return Response({'error': True, 'code': 'NOT_FOUND', 'message': f'Assignment not found for {doc_ref} page {page_number}'}, status=404)
        except PermissionError as e:
            return Response({'error': True, 'code': 'FORBIDDEN', 'message': str(e)}, status=403)
        except Exception as e:
            import traceback
            logger.error(f"Workspace API Error: {e}\n{traceback.format_exc()}")
            return Response({'error': True, 'code': 'SERVER_ERROR', 'message': str(e)}, status=500)

    @action(detail=False, methods=['get'], url_path=r'content/(?P<doc_ref>[^/.]+)/(?P<page_number>\d+)/preview')
    def preview_baked_pdf(self, request, doc_ref=None, page_number=None):
        """Returns the baked PDF for a specific page with current edits."""
        try:
            page = get_object_or_404(Page, document__doc_ref=doc_ref, page_number=page_number)
            from apps.processing.services.pdf_baking import PDFBakeService
            pdf_content = PDFBakeService.bake_page_edits(page)
            
            from django.http import HttpResponse
            response = HttpResponse(pdf_content, content_type='application/pdf')
            response['Content-Disposition'] = f'inline; filename="preview_{doc_ref}_p{page_number}.pdf"'
            return response
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    # POST /api/v1/processing/workspace/content/<doc_ref>/<page_number>/start/
    @action(detail=False, methods=['post'], url_path=r'content/(?P<doc_ref>[^/.]+)/(?P<page_number>\d+)/start')
    def start_processing(self, request, doc_ref=None, page_number=None):
        """Triggers the timestamp start for all pages in the user's block"""
        try:
            target_assignment = self._get_assignment(doc_ref, page_number, request.user)
            
            # Start ALL active assignments for this user on this document
            assignments = PageAssignment.objects.filter(
                document=target_assignment.document,
                resource__user=request.user,
                status=PageAssignmentStatus.ASSIGNED
            )
            
            now = timezone.now()
            with transaction.atomic():
                for a in assignments:
                    a.status = PageAssignmentStatus.IN_PROGRESS
                    if not a.processing_start_at:
                        a.processing_start_at = now
                    a.save()
                    
                    # Also update the Page model for consistency
                    page = a.page
                    if not page.processing_started_at:
                        page.processing_started_at = now
                        page.processing_start_date = now.date()
                        page.processing_start_time = now.time()
                        page.save(update_fields=['processing_started_at', 'processing_start_date', 'processing_start_time'])
            
            return Response({'status': 'started', 'processing_start_at': target_assignment.processing_start_at})
        except Exception as e:
            return Response({'error': True, 'code': 'ERROR', 'message': str(e)}, status=400)

    # POST /api/v1/processing/workspace/content/<doc_ref>/<page_number>/submit/
    @action(detail=False, methods=['post'], url_path=r'content/(?P<doc_ref>[^/.]+)/(?P<page_number>\d+)/submit')
    def submit_processing(self, request, doc_ref=None, page_number=None):
        """Finalizes the entire block assigned to this user for this document"""
        try:
            target_assignment = self._get_assignment(doc_ref, page_number, request.user)
            
            # 1. Fetch all assignments in this user's block for this doc
            assignments = PageAssignment.objects.filter(
                document=target_assignment.document,
                resource__user=request.user,
                status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
            )
            
            # 2. Trigger completion pipeline for each
            with transaction.atomic():
                for a in assignments:
                    ProcessingService.complete_assignment(a.id, request.user)
                    
            return Response({'status': 'submitted', 'block_completed': assignments.count()})
        except PermissionError as e:
            return Response({'error': True, 'code': 'FORBIDDEN', 'message': str(e)}, status=403)
        except PageAssignment.DoesNotExist:
            return Response({'error': True, 'code': 'NOT_FOUND', 'message': 'Target assignment not found'}, status=404)
        except Exception as e:
            logger.error(f"Submit API Error: {e}", exc_info=True)
            return Response({'error': True, 'code': 'ERROR', 'message': str(e)}, status=400)


class ProcessingAdminViewSet(viewsets.ViewSet):
    """
    Section 11 APIs for the Admin panel.
    """
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    # POST /api/admin/pages/<doc_ref>/<page_number>/review/
    @action(detail=False, methods=['post'], url_path=r'pages/(?P<doc_ref>[^/.]+)/(?P<page_number>\d+)/review')
    def review_page(self, request, doc_ref=None, page_number=None):
        action = request.data.get('action') # 'approve' or 'reject'
        
        submission = get_object_or_404(
            SubmittedPage,
            document__doc_ref=doc_ref,
            page_number=page_number
        )
        
        from common.enums import ReviewStatus
        with transaction.atomic():
            if action == 'approve':
                submission.review_status = ReviewStatus.APPROVED
                submission.reviewed_by = request.user
                submission.reviewed_at = timezone.now()
                submission.save() # Signal triggers document merge check
                return Response({'status': 'approved'})
                
            elif action == 'reject':
                submission.review_status = ReviewStatus.REJECTED
                submission.reviewed_by = request.user
                submission.reviewed_at = timezone.now()
                submission.review_notes = request.data.get('notes', '')
                submission.save()
                
                # Create RejectedPage record for this page
                from apps.processing.models import RejectedPage
                reason = request.data.get('reason', 'QUALITY_FAIL')
                rejected = RejectedPage.objects.create(
                    submission=submission,
                    document=submission.document,
                    page=submission.page,
                    page_number=submission.page_number,
                    rejected_by=request.user,
                    original_resource=submission.assignment.resource,
                    rejection_reason=reason,
                    rejection_notes=request.data.get('notes', '')
                )
                # Mark the original resource as excluded for this page
                from apps.accounts.models import ResourceProfile
                try:
                    res_profile = ResourceProfile.objects.get(user=submission.submitted_by)
                    rejected.excluded_resources.add(res_profile)
                except ResourceProfile.DoesNotExist:
                    logger.warning(f"No ResourceProfile found for user {submission.submitted_by.username} during rejection.")
                
                # === Block Rejection: Find all submitted pages from same resource+doc ===
                from apps.processing.models import SubmittedPage
                from common.enums import ReviewStatus as RS
                sibling_submissions = SubmittedPage.objects.filter(
                    document=submission.document,
                    submitted_by=submission.submitted_by,
                    review_status=RS.PENDING_REVIEW
                ).exclude(id=submission.id)
                
                # Reject siblings too
                for sibling in sibling_submissions:
                    sibling.review_status = RS.REJECTED
                    sibling.reviewed_by = request.user
                    sibling.reviewed_at = timezone.now()
                    sibling.review_notes = request.data.get('notes', 'Rejected as part of block')
                    sibling.save()
                    # Reassign sibling
                    AssignmentService.reassign_rejected_assignment(sibling.assignment.id, request.user)
                
                # Reassign primary
                AssignmentService.reassign_rejected_assignment(submission.assignment.id, request.user)
                total_rejected = 1 + sibling_submissions.count()
                return Response({'status': 'rejected_and_reassigned', 'pages_rejected': total_rejected})
                
        return Response({'error': 'Invalid action'}, status=400)

    # POST /api/v1/processing/admin/assignments/<id>/reject/
    @action(detail=False, methods=['post'], url_path=r'assignments/(?P<assignment_id>\d+)/reject')
    def reject_assignment(self, request, assignment_id=None):
        """Admin revokes an active assignment and triggers reassignment for the full block."""
        try:
            from apps.processing.models import PageAssignment
            
            # 1. Get the primary assignment being rejected
            primary = get_object_or_404(PageAssignment, id=assignment_id)
            
            # 2. Find ALL other active or submitted assignments for the same resource + document (the full block)
            block_assignments = PageAssignment.objects.filter(
                document=primary.document,
                resource=primary.resource,
                status__in=[
                    PageAssignmentStatus.ASSIGNED, 
                    PageAssignmentStatus.IN_PROGRESS,
                    PageAssignmentStatus.SUBMITTED
                ]
            ).order_by('page__page_number')
            
            block_ids = list(block_assignments.values_list('id', flat=True))
            logger.info(f"Block rejection: Rejecting {len(block_ids)} assignments for resource={primary.resource.user.username}, doc={primary.document.name}")
            
            # 3. Reassign all pages in the block
            from apps.processing.models import SubmittedPage
            from common.enums import ReviewStatus
            
            for aid in block_ids:
                try:
                    assignment = PageAssignment.objects.get(id=aid)
                    # If SUBMITTED, clean up the SubmittedPage record too
                    if assignment.status == PageAssignmentStatus.SUBMITTED:
                        sp = SubmittedPage.objects.filter(
                            assignment=assignment, 
                            review_status=ReviewStatus.PENDING_REVIEW
                        ).first()
                        if sp:
                            sp._skip_auto_reassign = True
                            sp.review_status = ReviewStatus.REJECTED
                            sp.reviewed_by = request.user
                            sp.reviewed_at = timezone.now()
                            sp.review_notes = request.data.get('notes', 'Rejected as part of block rejection')
                            from common.enums import RejectionReason
                            sp.rejection_reason = RejectionReason.MANUAL_OVERRIDE
                            sp.save()

                    # Use auto_assign=False to just reset everything first
                    AssignmentService.reassign_rejected_assignment(aid, request.user, auto_assign=False)
                except Exception as e:
                    logger.warning(f"Failed to reset assignment {aid}: {e}")

            # 4. Trigger the assignment engine ONCE for the whole block
            count = AssignmentService.assign_pages(primary.document.id)
            
            if count > 0:
                # Find the new resource assigned (just for message)
                new_asgn = PageAssignment.objects.filter(
                    document=primary.document, 
                    status=PageAssignmentStatus.ASSIGNED
                ).order_by('-assigned_at').first()
                new_resource = new_asgn.resource.user.username if new_asgn else "Next Person"
                
                return Response({
                    'status': 'success',
                    'message': f'Full block rejected. {count} page(s) reassigned to {new_resource}.',
                    'new_resource': new_resource,
                    'reassigned_count': count
                })
            else:
                return Response({
                    'status': 'success',
                    'message': f'Full block of {len(block_ids)} page(s) rejected and returned to queue.',
                    'reassigned_count': 0
                })
                
        except PageAssignment.DoesNotExist:
            return Response({'error': 'Assignment not found'}, status=404)
        except Exception as e:
            logger.error(f"Block rejection failed: {e}")
            return Response({'error': str(e)}, status=500)

    @action(detail=False, methods=['post'], url_path=r'assignments/(?P<assignment_id>\d+)/approve')
    def approve_assignment(self, request, assignment_id=None):
        """
        Admin approves a submitted assignment block.
        Marks all SUBMITTED pages in the block as APPROVED.
        """
        primary = get_object_or_404(PageAssignment, id=assignment_id)
        
        # Block Approval: Find all assignments for same resource + document that are SUBMITTED
        block_assignments = PageAssignment.objects.filter(
            document=primary.document,
            resource=primary.resource,
            status=PageAssignmentStatus.SUBMITTED
        )
        
        if not block_assignments.exists():
            return Response({'error': 'No submitted assignments found for this block.'}, status=400)

        from apps.processing.models import SubmittedPage
        from common.enums import ReviewStatus
        
        count = 0
        with transaction.atomic():
            for a in block_assignments:
                # 1. Update Assignment Status
                a.status = PageAssignmentStatus.APPROVED
                a.save()
                
                # 2. Update SubmittedPage status
                sp = SubmittedPage.objects.filter(assignment=a, review_status=ReviewStatus.PENDING_REVIEW).first()
                if sp:
                    sp.review_status = ReviewStatus.APPROVED
                    sp.reviewed_by = request.user
                    sp.reviewed_at = timezone.now()
                    sp.save() # This triggers document merge check via signal
                    count += 1
                
                # 3. Update Page status
                page = a.page
                from common.enums import PageStatus
                page.status = PageStatus.COMPLETED
                page.save()

        logger.info(f"Block Approval: {count} pages approved for resource {primary.resource.user.username} on doc {primary.document.name}")
        
        return Response({
            'status': 'success',
            'message': f'Approved {count} pages for {primary.resource.user.username}.',
            'approved_count': count
        })

    # POST /api/admin/documents/<doc_ref>/approve/
    @action(detail=False, methods=['post'], url_path=r'documents/(?P<doc_ref>[^/.]+)/approve')
    def approve_document(self, request, doc_ref=None):
        """Manual shortcut for force overriding documents if needed"""
        doc = get_object_or_404(Document, doc_ref=doc_ref)
        
        # Manual trigger of MergeService
        from apps.processing.tasks import merge_document_pages
        merge_document_pages.delay(doc.id, request.user.id)
        return Response({'status': 'triggered', 'message': 'Document merge triggered.'})

    # GET /api/admin/reassignment-log/
    @action(detail=False, methods=['get'], url_path='reassignment-log')
    def get_reassignments(self, request):
        logs = ReassignmentLog.objects.select_related(
            'previous_resource__user', 'new_resource__user', 'reassigned_by'
        ).order_by('-created_at')[:50]
        
        from apps.processing.serializers import ReassignmentLogSerializer
        return Response(ReassignmentLogSerializer(logs, many=True).data)

    # GET /api/v1/processing/admin/dashboard/
    @action(detail=False, methods=['get'], url_path='dashboard')
    def get_dashboard(self, request):
        from apps.accounts.models import User, ResourceProfile
        from apps.documents.models import Document, Page
        from common.enums import DocumentStatus, PageStatus, ResourceStatus
        from django.db.models import Count
        
        # 1. Document Stats
        total_docs = Document.objects.count()
        processing_docs = Document.objects.filter(status__in=[DocumentStatus.ASSIGNED, DocumentStatus.IN_PROGRESS]).count()
        pending_reviews = Document.objects.filter(status=DocumentStatus.REVIEWING).count()
        unassigned_docs = Document.objects.filter(status__in=[DocumentStatus.UPLOADED, DocumentStatus.SPLITTING]).count()
        
        # 2. User Stats
        total_users = User.objects.count()
        active_res = ResourceProfile.objects.filter(status=ResourceStatus.ACTIVE).count()
        busy_res = ResourceProfile.objects.filter(status=ResourceStatus.BUSY).count()
        
        # 3. Page Stats
        assigned_pages = Page.objects.filter(status=PageStatus.ASSIGNED).count()
        unassigned_pages = Page.objects.filter(status=PageStatus.PENDING).count()
        
        return Response({
            'total_docs': total_docs,
            'total_users': total_users,
            'processing_docs': processing_docs,
            'pending_reviews': pending_reviews,
            'assigned_pages_count': assigned_pages,
            'unassigned_pages_count': unassigned_pages,
            'unassigned_docs_count': unassigned_docs,
            'resources': {
                'active': active_res,
                'busy': busy_res,
                'total_online': active_res + busy_res
            }
        })

from django.shortcuts import render
from django.contrib.auth.decorators import login_required

@login_required
def workspace_view(request, doc_ref, page_number):
    """
    Renders the frontend workspace for a specific page assignment.
    Validates that the user has access to this assignment before rendering.
    """
    is_admin = request.user.role == UserRole.ADMIN or request.user.is_superuser or request.user.is_staff
    
    # 1. Try to find an assignment (Active or Submitted)
    # This view is mainly an entry point; the actual data comes from the API.
    # We just need to check if the user HAS A REASON to be here.
    
    try:
        # Check for assignment for this user (or any if admin)
        assignments = PageAssignment.objects.filter(
            document__doc_ref=doc_ref,
            page__page_number=page_number
        ).select_related('document', 'page', 'resource__user')
        
        if not is_admin:
            assignments = assignments.filter(resource__user=request.user)
            
        if not assignments.exists():
            # If no assignment, check if the page exists for admin view
            if is_admin:
                page = get_object_or_404(Page, document__doc_ref=doc_ref, page_number=page_number)
                context = {
                    'doc_ref': doc_ref,
                    'page_number': page_number,
                    'pdf_url': page.content_file.url if page.content_file else page.document.file.url,
                    'is_readonly': False, # Admins can edit
                    'assignment': None
                }
                return render(request, 'resource/edit_assignment.html', context)
            return render(request, 'error.html', {'message': 'Assignment not found.', 'code': 404}, status=404)
        
        # Take the "most relevant" assignment
        assignment = assignments.latest('assigned_at')
        
        # Determine read-only based on status OR role
        is_readonly = assignment.status not in [PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
        
        # Even if admin, if viewing a submitted page, it's read-only in the editor layer
        
        pdf_url = assignment.page.document.file.url if assignment.page.document.file else ''
        if assignment.page.content_file:    
            pdf_url = assignment.page.content_file.url
            
        context = {
            'doc_ref': doc_ref,
            'page_number': page_number,
            'assignment': assignment,
            'pdf_url': pdf_url,
            'is_readonly': is_readonly
        }
        return render(request, 'resource/edit_assignment.html', context)
        
    except Exception as e:
        logger.error(f"Workspace rendering error: {e}")
        return render(request, 'error.html', {'message': str(e), 'code': 500}, status=500)


# ── Section 11: Capacity & Rebalancing (New Spec) ──────────

class ResourceCapacityUpdateView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def patch(self, request, resource_id):
        resource = get_object_or_404(
            ResourceProfile.objects.select_for_update(),
            pk=resource_id
        )
        new_capacity = request.data.get('max_capacity')

        # Validate new capacity value
        if new_capacity is None:
            return Response(
                {'error': True, 'code': 'MISSING_FIELD',
                 'message': 'max_capacity is required'},
                status=400
            )

        try:
            new_capacity = int(new_capacity)
            if new_capacity < 1 or new_capacity > 100:
                raise ValueError()
        except (ValueError, TypeError):
            return Response(
                {'error': True, 'code': 'INVALID_VALUE',
                 'message': 'max_capacity must be an integer between 1 and 100'},
                status=400
            )

        old_capacity = resource.max_capacity

        with transaction.atomic():
            # ── Step 1: Update capacity ───────────────────────
            resource.max_capacity = new_capacity
            resource.save(update_fields=['max_capacity'])

            # ── Step 2: Recompute current load from DB ────────
            current_load   = resource.current_load
            remaining      = resource.remaining_capacity

            # ── Step 3: Refresh status ────────────────────────
            resource.refresh_status()

            # ── Step 4: Rebalance if capacity was reduced ─────
            rebalanced_pages = []
            if new_capacity < old_capacity and current_load > new_capacity:
                rebalanced_pages = rebalance_overloaded_resource(resource)

            # ── Step 5: Broadcast status update via WebSocket ─
            # ── Step 5: Broadcast status update via WebSocket ─
            broadcast_resource_status(resource)

        return Response({
            'success':          True,
            'resource_id':      resource_id,
            'username':         resource.user.username,
            'old_capacity':     old_capacity,
            'new_capacity':     new_capacity,
            'current_load':     round(current_load, 2),
            'remaining':        round(remaining, 2),
            'status':           resource.status,
            'rebalanced_pages': len(rebalanced_pages),
            'message':          f'Capacity updated from {old_capacity} '
                              f'to {new_capacity}. '
                              f'Current load: {round(current_load, 2)}. '
                              f'Remaining: {round(remaining, 2)}.',
        })


def rebalance_overloaded_resource(resource):
    """
    When capacity is reduced below current load:
    unassign excess pages (lowest priority first)
    and put them back in the assignment queue.
    """
    from apps.processing.models import PageAssignment
    from common.enums import PageAssignmentStatus, PageStatus
    
    overflow = resource.current_load - resource.max_capacity
    if overflow <= 0:
        return []

    # Get assigned pages ordered by lowest priority (unassign these first)
    excess_assignments = PageAssignment.objects.filter(
        resource=resource,
        status=PageAssignmentStatus.ASSIGNED          # only unassign not-yet-started pages
    ).select_related('page').order_by(
        '-page__complexity_weight'  # unassign heaviest first to fix faster
    )

    unassigned = []
    freed_weight = 0.0

    with transaction.atomic():
        for assignment in excess_assignments:
            if freed_weight >= overflow:
                break
            assignment.status = PageAssignmentStatus.UNASSIGNED
            assignment.save(update_fields=['status'])
            
            assignment.page.status = PageStatus.PENDING
            assignment.page.save(update_fields=['status'])
            
            freed_weight += assignment.page.complexity_weight or 1.0
            unassigned.append(assignment.page.id)

    # Re-queue unassigned pages for automatic reassignment
    if unassigned:
        from apps.processing.tasks import assign_pages_task
        assign_pages_task.delay()

    return unassigned


def broadcast_resource_status(resource):
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

# ── Section 12: Layout Overhaul API ────────────────────────

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from apps.documents.models import Page, Block, PageTable, BlockEdit
from apps.documents.serializers import BlockSerializer, PageTableSerializer

class PageBlocksAPIView(APIView):
    """
    Returns all blocks and tables for a page, with pre-computed CSS coordinates
    based on the requested container width/height.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request, page_id):
        page = get_object_or_404(Page, id=page_id)
        blocks = page.blocks.all().order_by('y', 'x')
        tables = page.tables.all()
        
        # Get target container dimensions from query params (default to common A4 px)
        css_w = float(request.query_params.get('width', 800))
        css_h = float(request.query_params.get('height', 1131))
        
        blocks_data = []
        for b in blocks:
            data = BlockSerializer(b).data
            # Injects 'css' property: {left, top, width, height, font_size}
            data['css'] = b.get_css_coords(css_w, css_h)
            blocks_data.append(data)
            
        return Response({
            'page_id': page_id,
            'pdf_width': page.pdf_page_width,
            'pdf_height': page.pdf_page_height,
            'blocks': blocks_data,
            'tables': PageTableSerializer(tables, many=True).data
        })


class BlockSaveView(APIView):
    """
    Atomic update for a text block.
    """
    permission_classes = [IsAuthenticated]
    
    def patch(self, request, block_id):
        block = get_object_or_404(Block, id=block_id)
        text = request.data.get('text', '')
        
        block.current_text = text
        block.is_dirty = True
        block.last_edited_by = request.user
        block.last_edited_at = timezone.now()
        block.save()
        
        # Log edit
        BlockEdit.objects.create(
            block=block,
            edited_by=request.user,
            text=text,
            page_num=block.page.page_number
        )
        
        return Response({'status': 'saved', 'block_id': block_id})


class TableCellSaveView(APIView):
    """
    Update a specific table cell. 
    Finds the block matching table_id, row, and col.
    """
    permission_classes = [IsAuthenticated]
    
    def patch(self, request, table_id):
        row = request.data.get('row')
        col = request.data.get('col')
        text = request.data.get('text', '')
        
        # Find the specific block
        block = get_object_or_404(Block, table_id=table_id, row_index=row, col_index=col)
        
        block.current_text = text
        block.is_dirty = True
        block.last_edited_by = request.user
        block.last_edited_at = timezone.now()
        block.save()
        
        return Response({'status': 'saved'})

# ── Real-time Auto-refresh Views ──────────────────────────────────────────

@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated])
def heartbeat(request):
    """
    Resource person sends POST every 15 seconds.
    Updates last_seen timestamp and current page.
    Returns their current assignment count.
    """
    try:
        profile = request.user.resource_profile
    except ResourceProfile.DoesNotExist:
        return Response({'error': 'Resource profile not found'}, status=404)

    profile.last_seen = timezone.now()
    profile.last_seen_page = request.data.get('current_page', '')

    # Auto-set ACTIVE when heartbeat received if it was INACTIVE
    if profile.status == ResourceStatus.INACTIVE:
        profile.status = ResourceStatus.ACTIVE
        profile.is_available = True

    profile.save(update_fields=['last_seen', 'last_seen_page', 'status', 'is_available'])

    # Return current assignment info for the resource
    assigned_count = PageAssignment.objects.filter(
        resource=profile,
        status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
    ).count()

    return Response({
        'status':          'online',
        'assigned_pages':  assigned_count,
        'current_load':    profile.get_current_load(),
        'remaining':       profile.get_remaining_capacity(),
        'server_time':     timezone.now().isoformat(),
    })

class ResourceStatusListView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        """
        Returns all resources with live online status.
        Admin polls this every 10 seconds.
        """
        resources = ResourceProfile.objects.select_related('user').all()

        data = []
        for r in resources:
            data.append({
                'id':             r.pk,
                'username':       r.user.username,
                'full_name':      r.user.get_full_name(),
                'online_status':  r.online_status,
                'is_online':      r.is_online,
                'status':         r.status,
                'last_seen':      r.last_seen.isoformat() if r.last_seen else None,
                'current_load':   r.get_current_load(),
                'max_capacity':   r.max_capacity,
                'remaining':      r.get_remaining_capacity(),
                'assigned_pages': PageAssignment.objects.filter(
                    resource=r,
                    status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
                ).count(),
            })

        return Response({
            'resources':    data,
            'online_count': sum(1 for r in data if r['is_online']),
            'total':        len(data),
            'polled_at':    timezone.now().isoformat(),
        })

class DocumentListRefreshView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        """
        Returns recent documents with pipeline status.
        Admin polls every 5 seconds to catch conversions / splits.
        """
        docs = Document.objects.order_by('-created_at')[:50]

        data = []
        for doc in docs:
            total = doc.total_pages or 0
            assigned = Page.objects.filter(
                document=doc,
                status=PageStatus.ASSIGNED
            ).count() if total > 0 else 0
            submitted = SubmittedPage.objects.filter(
                page__document=doc
            ).count() if total > 0 else 0
            approved = SubmittedPage.objects.filter(
                page__document=doc,
                review_status=ReviewStatus.APPROVED
            ).count() if total > 0 else 0

            data.append({
                'id':                 doc.pk,
                'doc_ref':            doc.doc_ref,
                'title':              doc.title or doc.name,
                'pipeline_status':    doc.pipeline_status,
                'conversion_status':  doc.conversion_status,
                'total_pages':        total,
                'assigned_pages':     assigned,
                'submitted_pages':    submitted,
                'approved_pages':     approved,
                'progress_pct': round((approved / total * 100) if total > 0 else 0, 1),
                'uploaded_at':  doc.created_at.isoformat(),
                'uploaded_by':  doc.client.username,
            })

        return Response({
            'documents': data,
            'polled_at': timezone.now().isoformat(),
        })

class AssignmentQueueView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        """
        Admin:    returns full unassigned page queue
        Resource: returns their own assigned pages
        """
        user = request.user
        is_admin = user.role == UserRole.ADMIN or user.is_superuser or user.is_staff

        if is_admin:
            # Admin view — unassigned queue
            unassigned = Page.objects.filter(
                status=PageStatus.UNASSIGNED,
                is_validated=True,
            ).select_related('document').order_by('document__priority', 'page_number')

            queue_data = [{
                'id':          p.pk,
                'doc_ref':     p.document.doc_ref,
                'doc_title':   p.document.title or p.document.name,
                'page_number': p.page_number,
                'complexity':  p.complexity_weight, # Using weight as proxy for complexity string if needed
                'weight':      p.complexity_weight,
                'priority':    p.document.priority,
            } for p in unassigned]

            return Response({
                'role':          'admin',
                'queue':         queue_data,
                'queue_count':   len(queue_data),
                'polled_at':     timezone.now().isoformat(),
            })
        else:
            # Resource view — their assigned pages
            try:
                profile = user.resource_profile
            except ResourceProfile.DoesNotExist:
                return Response({'assignments': [], 'queue_count': 0})

            assignments = PageAssignment.objects.filter(
                resource=profile,
                status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
            ).select_related('page', 'page__document').order_by('page__document__priority', 'page__page_number')

            data = [{
                'assignment_id': a.pk,
                'doc_ref':       a.page.document.doc_ref,
                'doc_title':     a.page.document.title or a.page.document.name,
                'page_number':   a.page.page_number,
                'complexity':    a.page.complexity_weight,
                'status':        a.status,
                'max_time':      a.max_processing_time,
                'started_at':    a.processing_start_at.isoformat() if a.processing_start_at else None,
                'workspace_url': f'/workspace/{a.page.document.doc_ref}/{a.page.page_number}/',
            } for a in assignments]

            return Response({
                'role':         'resource',
                'assignments':  data,
                'queue_count':  len(data),
                'current_load': profile.get_current_load(),
                'remaining':    profile.get_remaining_capacity(),
                'polled_at':    timezone.now().isoformat(),
            })

class SubmittedPagesQueueView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        """
        Returns submitted pages pending admin review.
        Admin polls every 5 seconds.
        """
        pending = SubmittedPage.objects.filter(
            review_status=ReviewStatus.PENDING_REVIEW
        ).select_related('page', 'page__document', 'submitted_by').order_by('submitted_at')

        data = [{
            'id':            s.pk,
            'doc_ref':       s.page.document.doc_ref,
            'doc_title':     s.page.document.title or s.page.document.name,
            'page_number':   s.page.page_number,
            'submitted_by':  s.submitted_by.username,
            'submitted_at':  s.submitted_at.isoformat(),
            'review_url':    f'/admin/review/{s.page.document.doc_ref}/{s.page.page_number}/',
        } for s in pending]

        return Response({
            'pending_review': data,
            'pending_count':  len(data),
            'polled_at':      timezone.now().isoformat(),
        })

class AdminDashboardSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated, permissions.IsAdminUser]

    def get(self, request):
        """
        Single endpoint that returns everything the admin dashboard
        needs. Poll this every 5 seconds instead of 5 separate calls.
        """
        # Resource status counts
        all_resources = ResourceProfile.objects.all()
        resources_summary = {
            'online':  sum(1 for r in all_resources if r.online_status == 'online'),
            'away':    sum(1 for r in all_resources if r.online_status == 'away'),
            'offline': sum(1 for r in all_resources if r.online_status == 'offline'),
            'total':   all_resources.count(),
        }

        # Document pipeline counts
        pipeline_counts = {}
        for status_value, status_label in PipelineStatus.choices:
            pipeline_counts[status_value] = Document.objects.filter(pipeline_status=status_value).count()

        # Queue stats
        queue_stats = {
            'unassigned_pages': Page.objects.filter(
                status=PageStatus.UNASSIGNED,
                is_validated=True,
            ).count(),
            'in_progress_pages': PageAssignment.objects.filter(
                status=PageAssignmentStatus.IN_PROGRESS
            ).count(),
            'pending_review': SubmittedPage.objects.filter(
                review_status=ReviewStatus.PENDING_REVIEW
            ).count(),
        }

        return Response({
            'resources':    resources_summary,
            'pipeline':     pipeline_counts,
            'queue':        queue_stats,
            'polled_at':    timezone.now().isoformat(),
        })
