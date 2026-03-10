
from rest_framework import serializers
from apps.documents.models import Document, Page, Block, BlockEdit, PageTable

class BlockSerializer(serializers.ModelSerializer):
    class Meta:
        model = Block
        fields = [
            'id', 'block_index', 'block_id', 'block_type',
            'original_text', 'current_text', 'is_dirty',
            'x', 'y', 'width', 'height', 'bbox',
            'font_name', 'font_size', 'font_weight', 'font_style', 'font_color',
            'table_id', 'row_index', 'col_index'
        ]

class PageTableSerializer(serializers.ModelSerializer):
    class Meta:
        model = PageTable
        fields = [
            'id', 'table_ref', 'x', 'y', 'width', 'height',
            'row_count', 'col_count', 'table_json', 'has_borders', 'has_header'
        ]

class PageSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    complexity_type_display = serializers.CharField(source='get_complexity_type_display', read_only=True)
    validation_status_display = serializers.CharField(source='get_validation_status_display', read_only=True)
    
    blocks = BlockSerializer(many=True, read_only=True)
    tables = PageTableSerializer(many=True, read_only=True)

    class Meta:
        model = Page
        fields = (
            'id', 'page_number', 'status', 'status_display', 
            'current_assignee', 'locked_at', 'text_content',
            # Layout data
            'pdf_page_width', 'pdf_page_height', 'blocks_extracted', 'blocks_count', 'has_tables',
            'blocks', 'tables',
            # Complexity Data
            'complexity_type', 'complexity_type_display', 'complexity_weight',
            'table_count', 'image_count', 'word_count',
            # Processing meta
            'processing_started_at', 'processing_start_date', 'processing_start_time',
            'processing_completed_at', 'processing_end_date', 'processing_end_time',
            'processing_duration_seconds', 'total_time_spent',
            'is_processed', 'is_scanned',
            # Validation Data
            'validation_status', 'validation_status_display', 'validation_errors'
        )
        read_only_fields = (
            'current_assignee', 'locked_at',
            'complexity_type', 'complexity_weight', 'table_count', 'image_count', 'word_count',
            'processing_started_at', 'processing_start_date', 'processing_start_time',
            'processing_completed_at', 'processing_end_date', 'processing_end_time',
            'processing_duration_seconds', 'total_time_spent',
            'is_processed', 'is_scanned',
            'validation_status', 'validation_errors'
        )

class DocumentSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    pipeline_status_display = serializers.CharField(source='get_pipeline_status_display', read_only=True)
    pages = PageSerializer(many=True, read_only=True)
    assigned_resources = serializers.SerializerMethodField()
    granular_status = serializers.SerializerMethodField()
    
    class Meta:
        model = Document
        fields = (
            'id', 'doc_ref', 'title', 'name', 'original_file', 'file', 'converted_pdf', 
            'status', 'status_display', 'pipeline_status', 'pipeline_status_display',
            'priority', 'deadline', 'conversion_error',
            'total_pages', 'created_at', 'updated_at', 'completed_at', 
            'final_file', 'pages', 'assigned_resources', 'granular_status',
            'version', 'completion_percentage'
        )
        read_only_fields = ('doc_ref', 'status', 'pipeline_status', 'total_pages', 'completed_at', 'final_file', 'version', 'completion_percentage')

    def get_assigned_resources(self, obj):
        try:
            from apps.processing.models import PageAssignment
            # Use new PageAssignment model instead of legacy Block assignment
            assignments = PageAssignment.objects.filter(document=obj).select_related('resource__user', 'page').order_by('page__page_number')
            
            # Group assignments by resource to provide a consolidated "Single PDF" view
            resource_groups = {}
            from django.core.cache import cache
            from datetime import timedelta
            
            for a in assignments:
                if not a.resource or not hasattr(a.resource, 'user') or not a.resource.user:
                    continue
                
                res_id = a.resource.user.id
                # Grouping key is the resource ID
                if res_id not in resource_groups:
                    is_online = cache.get(f"user:{res_id}:online") == "true"
                    resource_groups[res_id] = {
                        'id': a.id, # Base ID for reference
                        'username': a.resource.user.username,
                        'pages': [a.page.page_number],
                        'status': a.get_status_display(),
                        'status_raw': a.status,
                        'viewed_at': a.processing_start_at,
                        'completed_at': a.submitted_at or a.processing_end_at,
                        'is_online': is_online,
                        'assigned_at': a.assigned_at,
                        'max_time': a.max_processing_time if hasattr(a, 'max_processing_time') else 600
                    }
                else:
                    # Append page and update latest status
                    group = resource_groups[res_id]
                    group['pages'].append(a.page.page_number)
                    # If any part of the block is in progress, show that
                    if a.status == 'IN_PROGRESS' and group['status_raw'] != 'IN_PROGRESS':
                        group['status'] = a.get_status_display()
                        group['status_raw'] = a.status
                    if a.processing_start_at and (not group['viewed_at'] or a.processing_start_at < group['viewed_at']):
                        group['viewed_at'] = a.processing_start_at
                    if a.submitted_at and (not group['completed_at'] or a.submitted_at > group['completed_at']):
                        group['completed_at'] = a.submitted_at
            
            results = []
            for res_id, group in resource_groups.items():
                pages = sorted(list(set(group['pages'])))
                if not pages: continue
                
                if len(pages) > 1 and pages[-1] - pages[0] == len(pages) - 1:
                    page_str = f"{pages[0]}-{pages[-1]}"
                else:
                    page_str = ", ".join(map(str, pages))
                    
                expires_at = None
                if not group['completed_at'] and group['assigned_at']:
                    # Ensure max_time is int
                    try:
                        m_time = int(group['max_time'])
                    except:
                        m_time = 600
                    expires_at = group['assigned_at'] + timedelta(seconds=m_time)
                
                results.append({
                    'id': group['id'],
                    'username': group['username'],
                    'page_number': page_str,
                    'start_page': pages[0],
                    'end_page': pages[-1],
                    'status': group['status'],
                    'status_raw': group['status_raw'],
                    'viewed_at': group['viewed_at'],
                    'completed_at': group['completed_at'],
                    'expires_at': expires_at,
                    'is_online': group['is_online']
                })
            return results
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in get_assigned_resources: {str(e)}", exc_info=True)
            return []

    def get_granular_status(self, obj):
        return obj.get_pipeline_status_display()
        

class DocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()

    def validate_file(self, value):
        import os
        from pathlib import Path
        ext = Path(value.name).suffix.lower()
        
        # 1. Extension Check
        allowed = ['.pdf', '.docx', '.doc']
        if ext not in allowed:
            raise serializers.ValidationError(
                f"Unsupported format '{ext}'. Allowed: PDF, DOCX, DOC"
            )

        # 2. Size Check (100MB)
        max_size = 100 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError(
                f"File size {value.size // (1024*1024)}MB exceeds 100MB limit."
            )
            
        return value
