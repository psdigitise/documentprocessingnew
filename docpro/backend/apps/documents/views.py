
from rest_framework import viewsets, permissions, status, parsers, views
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from apps.documents.models import Document, Page, Block, BlockEdit
from apps.documents.serializers import DocumentSerializer, DocumentUploadSerializer, PageSerializer, BlockSerializer
from apps.documents.services import DocumentService
from common.enums import UserRole
from common.validators import StatusTransitionValidator
from django.core.cache import cache

class DocumentViewSet(viewsets.ModelViewSet):
    queryset = Document.objects.all()
    serializer_class = DocumentSerializer
    parser_classes = (parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser)

    def get_queryset(self):
        user = self.request.user
        if user.role == 'CLIENT':
            return Document.active.filter(client=user)
        
        # Resources should only see documents they are/were assigned to
        if user.role == 'RESOURCE' and not (user.is_staff or user.is_superuser):
            from apps.processing.models import PageAssignment
            assigned_doc_ids = PageAssignment.objects.filter(
                resource__user=user
            ).values_list('document_id', flat=True).distinct()
            return Document.active.filter(id__in=assigned_doc_ids)
            
        return Document.active.all()

    def destroy(self, request, *args, **kwargs):
        """
        Hard delete implementation with thorough cascading cleanup.
        Satisfies user requirement: "archive/delete from assigned resource, database, and queue".
        """
        from apps.processing.models import PageAssignment, DocumentQueue, SubmittedPage
        from apps.audit.models import AuditLog
        
        document = self.get_object()
        doc_id = document.id
        doc_title = document.title or document.name
        
        # 1. Broad Manual Cleanup for un-linked or loosely linked items
        AuditLog.objects.filter(document_id=doc_id).delete()
        
        # 2. Physical File Cleanup from disk (satisfies backend deletion requirement)
        import os
        from django.conf import settings
        
        # Helper to safely delete file
        def safe_delete_file(file_obj):
            if file_obj and hasattr(file_obj, 'path') and os.path.exists(file_obj.path):
                try:
                    os.remove(file_obj.path)
                except Exception as e:
                    logger.error(f"Failed to delete file {file_obj.path}: {e}")

        # Delete all page-level split files
        for p in document.pages.all():
            safe_delete_file(p.content_file)
            
        # Delete submitted page blobs
        from apps.processing.models import SubmittedPage
        for sp in SubmittedPage.objects.filter(document=document):
            safe_delete_file(sp.output_page_file)
            
        # Delete merged document if exists
        from apps.processing.models import MergedDocument
        try:
            md = MergedDocument.objects.get(document=document)
            safe_delete_file(md.merged_file)
        except MergedDocument.DoesNotExist:
            pass

        # Delete document originals and processing files
        safe_delete_file(document.original_file)
        safe_delete_file(document.file)
        safe_delete_file(document.converted_pdf)
        safe_delete_file(document.final_file)

        # 3. Hard Delete the document itself (cascades database-level objects)
        document.delete()
        
        return Response({
            'status': 'deleted', 
            'message': f"Document '{doc_title}' and all associated assignments, queue entries, and audit logs have been permanently removed."
        }, status=status.HTTP_200_OK)


    def get_serializer_class(self):
        if self.action == 'create':
            return DocumentUploadSerializer
        return DocumentSerializer

    def create(self, request, *args, **kwargs):
        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            
            # Create document via service
            doc = DocumentService.create_document(request.user, serializer.validated_data['file'])
            
            # Trigger processing task
            import os
            ext = os.path.splitext(doc.original_file.name)[1].lower()
            
            # Trigger processing task in background to keep upload response fast
            from common.utils import run_task_background
            if ext in ['.docx', '.doc']:
                from apps.documents.tasks import convert_word_to_pdf
                run_task_background(lambda: convert_word_to_pdf.delay(doc.id))
            else:
                from apps.documents.tasks import split_document_task
                run_task_background(lambda: split_document_task.delay(doc.id))
            
            headers = self.get_success_headers(serializer.data)
            # Pass context for absolute URLs (crucial for nested FileFields)
            # Use doc from DB to ensure all fields like doc_ref are populated
            doc.refresh_from_db()
            response_serializer = DocumentSerializer(doc, context=self.get_serializer_context())
            return Response(response_serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Upload failed: {str(e)}", exc_info=True)
            return Response(
                {"error": str(e), "detail": "Internal server error during upload."}, 
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    @action(detail=True, methods=['get'], url_path='unassigned-pages')
    def get_unassigned_pages(self, request, pk=None):
        doc = self.get_object()
        from apps.processing.models import PageAssignment
        from common.enums import PageAssignmentStatus
        
        # Get all page numbers
        all_pages = set(doc.pages.values_list('page_number', flat=True))
        
        # Get assigned page numbers
        assigned_pages = set(PageAssignment.objects.filter(
            document=doc,
            status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS, PageAssignmentStatus.SUBMITTED]
        ).values_list('page__page_number', flat=True))
        
        unassigned = sorted(list(all_pages - assigned_pages))
        
        return Response({
            'doc_ref': doc.doc_ref,
            'total_pages': doc.total_pages,
            'unassigned_count': len(unassigned),
            'unassigned_pages': unassigned
        })

    @action(detail=True, methods=['get'], url_path='progress')
    def get_progress(self, request, pk=None):
        doc = self.get_object()
        from apps.processing.models import PageAssignment, SubmittedPage
        from common.enums import PageAssignmentStatus, ReviewStatus
        
        total = doc.total_pages or 0
        if total == 0:
            return Response({'progress': 0, 'status': doc.pipeline_status})
            
        # Approved is 100% complete for that page
        approved = SubmittedPage.objects.filter(document=doc, review_status=ReviewStatus.APPROVED).count()
        
        # Submitted is 80% complete 
        submitted = SubmittedPage.objects.filter(document=doc, review_status=ReviewStatus.PENDING_REVIEW).count()
        
        # In Progress is 40% complete
        processing = PageAssignment.objects.filter(document=doc, status=PageAssignmentStatus.IN_PROGRESS).count()
        
        # Assigned is 10% complete
        assigned = PageAssignment.objects.filter(document=doc, status=PageAssignmentStatus.ASSIGNED).count()
        
        # Weighted score
        score = (approved * 1.0) + (submitted * 0.8) + (processing * 0.4) + (assigned * 0.1)
        percentage = round((score / total) * 100, 1)
        
        return Response({
            'doc_ref': doc.doc_ref,
            'pipeline_status': doc.pipeline_status,
            'completion_percentage': min(100.0, percentage),
            'pages_approved': approved,
            'pages_submitted_pending_review': submitted,
            'pages_processing': processing,
            'pages_assigned_not_started': assigned,
            'total_pages': total
        })

    @action(detail=True, methods=['post'])
    def retry(self, request, pk=None):
        doc = self.get_object()
        from common.enums import DocumentStatus, PipelineStatus
        
        # Reset bits
        doc.status = DocumentStatus.UPLOADED
        doc.pipeline_status = PipelineStatus.UPLOADED
        doc.pipeline_error = ""
        doc.conversion_error = ""
        doc.save()
        
        # Cleanup old assignments for this document
        from apps.processing.models import PageAssignment
        PageAssignment.objects.filter(document=doc).delete()
        
        # Logic from create() to trigger processing
        import os
        ext = os.path.splitext(doc.original_file.name)[1].lower()
        
        if ext in ['.docx', '.doc']:
            from apps.documents.tasks import convert_word_to_pdf
            convert_word_to_pdf.delay(doc.id)
        else:
            # For PDF, if it already had pages, split_document service handles idempotency
            # but we want to force re-processing if it failed.
            # If doc.file is missing but it's a PDF, we might need to restore it from original_file
            if not doc.file and ext == '.pdf':
                doc.file = doc.original_file
                doc.save()
                
            from apps.documents.tasks import split_document_task
            split_document_task.delay(doc.id)
            
        return Response({'status': 'processing_restarted'})

    @action(detail=True, methods=['get'], url_path='assign-all')
    def assign_all(self, request, pk=None):
        doc = self.get_object()
        from common.enums import PipelineStatus
        
        if doc.pipeline_status not in [PipelineStatus.PENDING, PipelineStatus.PROCESSING]:
            return Response({'error': 'Document not in assignable state'}, status=400)
            
        from apps.processing.services.core import AssignmentService
        try:
            pages_assigned = AssignmentService.assign_pages()
            doc.refresh_from_db()
            return Response({
                'status': 'triggered', 
                'pages_recently_assigned_system_wide': pages_assigned,
                'pipeline_status': doc.pipeline_status
            })
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['get'])
    def pages(self, request, pk=None):
        doc = self.get_object()
        pages = doc.pages.all().order_by('page_number')
        
        # Verify no gaps
        page_numbers = list(pages.values_list('page_number', flat=True))
        expected = list(range(1, (doc.total_pages or 0) + 1))
        missing = sorted(set(expected) - set(page_numbers))
        
        # We use a nested serializer or just the PageSerializer
        from apps.documents.serializers import PageSerializer
        
        return Response({
            'doc_ref': doc.doc_ref,
            'total_pages': doc.total_pages,
            'pages_found': pages.count(),
            'missing_pages': missing,
            'pages': PageSerializer(pages, many=True).data
        })

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAdminUser])
    def approve(self, request, pk=None):
        doc = self.get_object()
        from common.enums import PipelineStatus
        from apps.processing.services.merge import MergeService
        
        if doc.pipeline_status in [PipelineStatus.MERGED, PipelineStatus.APPROVED] and doc.final_file:
            return Response({'message': 'Document already merged/approved and file is ready.'})
            
        if doc.pipeline_status not in [PipelineStatus.ALL_SUBMITTED, PipelineStatus.MERGED, PipelineStatus.APPROVED]:
            return Response({'error': f'Document in state {doc.pipeline_status}. All pages must be submitted/approved first.'}, status=400)
            
        try:
            MergeService.merge_approved_pages(doc, request.user.id)
            return Response({'status': 'approved_and_merged'})
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['get'])
    def download(self, request, pk=None):
        doc = self.get_object()
        
        final_file = None
        if hasattr(doc, 'merged_document') and doc.merged_document and doc.merged_document.merged_file:
            final_file = doc.merged_document.merged_file
        elif doc.final_file:
            final_file = doc.final_file
            
        if not final_file:
            return Response({'error': 'Final file not ready.'}, status=status.HTTP_400_BAD_REQUEST)
        
        from common.utils import SigningService
        signed_url = SigningService.sign_url(final_file.url, user_id=request.user.id)
        
        from apps.audit.models import AuditLog
        from common.enums import AuditEventType
        AuditLog.objects.create(
            action=AuditEventType.DOC_DOWNLOADED,
            document_id=doc.id,
            actor=request.user,
            metadata={'signed_url': signed_url}
        )
        
        return Response({'url': signed_url})

class PageViewSet(viewsets.ModelViewSet):
    queryset = Page.objects.all()
    serializer_class = PageSerializer
    permission_classes = [permissions.IsAuthenticated]

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAdminUser])
    def reassign(self, request, pk=None):
        """Manually reassign a page to a specific user"""
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id required'}, status=400)
            
        from apps.accounts.models import ResourceProfile
        from apps.processing.models import PageAssignment
        from common.enums import PageAssignmentStatus
        from django.db import transaction
        
        page = self.get_object()
        target_res = get_object_or_404(ResourceProfile, user_id=user_id)
        
        with transaction.atomic():
            # 1. Cancel existing active assignments
            PageAssignment.objects.filter(
                page=page,
                status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
            ).update(status=PageAssignmentStatus.REASSIGNED)
            
            # 2. Create new assignment
            new_asgn = PageAssignment.objects.create(
                page=page,
                resource=target_res,
                document=page.document,
                status=PageAssignmentStatus.ASSIGNED,
                max_processing_time=600 # Default
            )
            
            # 3. Update Page
            from common.enums import PageStatus
            page.status = PageStatus.ASSIGNED
            page.current_assignee = target_res.user
            page.save()
            
            # 4. Update Resource Load
            target_res.current_load += 1
            target_res.save(update_fields=['current_load'])
            
        return Response({'status': 'reassigned', 'resource': target_res.user.username})

    @action(detail=True, methods=['post'], permission_classes=[permissions.IsAdminUser])
    def reject(self, request, pk=None):
        from common.enums import PageStatus, AuditEventType
        from apps.audit.models import AuditLog
        
        page = self.get_object()
        page.status = PageStatus.IMPROPERLY_PROCESSED
        page.is_validated = False
        page.save()
        
        AuditLog.objects.create(
            action=AuditEventType.COMPLETED,
            document_id=page.document.id,
            actor=request.user,
            old_status=PageStatus.COMPLETED,
            new_status=PageStatus.IMPROPERLY_PROCESSED,
            metadata={'reason': request.data.get('reason', 'Quality issues'), 'page_id': page.id}
        )
        return Response({'status': 'rejected'})

class BlockUpdateView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, block_id):
        """
        Granularly save a single block's text.
        Updates Block.current_text and creates a BlockEdit record.
        """
        block = get_object_or_404(Block, id=block_id)

        # Security Check: Must be the active assignee or admin
        is_admin = request.user.role == UserRole.ADMIN or request.user.is_superuser or request.user.is_staff
        if not is_admin:
            from apps.processing.models import PageAssignment, PageAssignmentStatus
            has_active = PageAssignment.objects.filter(
                page=block.page,
                resource__user=request.user,
                status__in=[PageAssignmentStatus.ASSIGNED, PageAssignmentStatus.IN_PROGRESS]
            ).exists()
            if not has_active:
                return Response({'error': 'Permission denied. Page not assigned or already submitted.'}, status=403)

        new_text = request.data.get('text')
        
        if new_text is None:
            return Response({"error": "No text provided"}, status=status.HTTP_400_BAD_REQUEST)
            
        from django.db import transaction
        with transaction.atomic():
            # 1. Update Block
            block.current_text = new_text
            block.save()
            
            # 2. Create Audit Record
            BlockEdit.objects.create(
                block=block,
                edited_by=request.user,
                text=new_text,
                page_num=block.page.page_number
            )
            
        return Response({"status": "saved", "block_id": block_id})

class ConversionRetryView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, document_id):
        from apps.documents.models import Document
        from common.enums import ConversionStatus, PipelineStatus
        doc = get_object_or_404(Document, pk=document_id)

        if doc.conversion_status not in [ConversionStatus.CONVERSION_FAILED]:
            return Response(
                {'error': True, 'code': 'INVALID_STATE',
                 'message': 'Document is not in a failed state'},
                status=400
            )

        doc.conversion_status = ConversionStatus.PENDING
        doc.conversion_error = ''
        doc.pipeline_status = PipelineStatus.CONVERTING
        doc.save()

        from apps.documents.tasks import convert_word_to_pdf
        task = convert_word_to_pdf.delay(doc.id)
        doc.celery_task_id = task.id
        doc.save(update_fields=['celery_task_id'])

        return Response({
            'success': True,
            'document_id': document_id,
            'websocket_url': f'/ws/conversion/{document_id}/',
            'message': 'Conversion retry started.',
        })

class ConversionStatusView(views.APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, document_id):
        from apps.documents.models import Document
        doc = get_object_or_404(Document, pk=document_id)
        return Response({
            'document_id': doc.id,
            'conversion_status': doc.conversion_status,
            'pipeline_status': doc.pipeline_status,
            'conversion_error': doc.conversion_error,
            'started_at': doc.conversion_started_at,
            'completed_at': doc.conversion_completed_at,
            'doc_ref': doc.doc_ref,
        })
