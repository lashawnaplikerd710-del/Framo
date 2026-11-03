"""Shared defaults for formal product-ad montage jobs."""
from __future__ import annotations

import copy


FORMAL_AD_REQUIRED_NATIVE_CATEGORIES = (
    'music', 'sound', 'sticker', 'transition', 'effect', 'filter', 'text',
)

FORMAL_AD_MINIMUM_NATIVE_COUNTS = {
    'music': 1,
    'sound': 2,
    'sticker': 1,
    'transition': 2,
    'effect': 1,
    'filter': 1,
    'text': 2,
}

_FORMAL_AD_POLICY = {
    'schema_version': 'formal-ad-v1',
    'formal_ad': True,
    'new_narration_required': True,
    'selling_copy_required': True,
    'music_required': True,
    'sound_effects_required': True,
    'transitions_required': True,
    'rhythm_changes_required': True,
    'zoom_required': True,
    'shot_selection_required': True,
    'shot_reorder_allowed': True,
    'process_log_required': True,
    'official_resources_only': True,
    'required_native_categories': list(FORMAL_AD_REQUIRED_NATIVE_CATEGORIES),
    'minimum_native_counts': dict(FORMAL_AD_MINIMUM_NATIVE_COUNTS),
}


def policy_for(workflow: str):
    """Return an independent policy object for a workflow."""
    if workflow in ('product_ad', 'custom'):
        return copy.deepcopy(_FORMAL_AD_POLICY)
    if workflow == 'crossborder_montage':
        return {
            'schema_version': 'formal-ad-v1',
            'formal_ad': True,
            'mode': 'crossborder_copy_only_ad',
            'new_narration_required': False,
            'selling_copy_required': True,
            'music_required': True,
            'sound_effects_required': True,
            'sticker_required': True,
            'transitions_required': True,
            'effects_required': True,
            'filters_required': True,
            'rhythm_changes_required': True,
            'zoom_required': True,
            'shot_selection_required': True,
            'shot_reorder_allowed': True,
            'process_log_required': True,
            'official_resources_only': True,
            'required_native_categories': list(FORMAL_AD_REQUIRED_NATIVE_CATEGORIES),
            'minimum_native_counts': dict(FORMAL_AD_MINIMUM_NATIVE_COUNTS),
        }
    return {
        'schema_version': 'formal-ad-v1',
        'formal_ad': False,
        'new_narration_required': False,
        'selling_copy_required': False,
        'music_required': False,
        'sound_effects_required': False,
        'transitions_required': False,
        'rhythm_changes_required': False,
        'zoom_required': False,
        'shot_selection_required': False,
        'shot_reorder_allowed': True,
        'process_log_required': True,
        'official_resources_only': True,
        'required_native_categories': [],
        'minimum_native_counts': {},
    }

