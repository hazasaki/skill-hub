"""Derived authored metadata, never a replacement resource body."""
import hashlib
import json

FIELDS = ('capability', 'use_when', 'examples', 'aliases', 'not_for',
          'required_inputs', 'requirements', 'produces')


def build_card(item):
    body = item['body']
    raw = hashlib.sha256(body.encode('utf-8')).hexdigest()
    if item.get('file_sha256') != raw:
        raise ValueError('CARD_SOURCE_CHANGED: raw source identity mismatch')
    meta = item.get('metadata') or {}
    fields = {key: {'value': meta.get(key), 'missing': key not in meta,
                    'origin': 'authored_frontmatter.' + key if key in meta else None}
              for key in FIELDS}
    card = {'schema_version': 'skill_hub.resource_card.v1', 'source_raw_sha256': raw,
            'source_sha256': item['content_sha256'], 'fields': fields,
            'retrieval_text': json.dumps({key: fields[key]['value'] for key in FIELDS[:4]}, ensure_ascii=False, sort_keys=True),
            'index_coverage': 'request-local card; pinned retrieval indexes not rebuilt'}
    card['card_sha256'] = hashlib.sha256(json.dumps(card, ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return card


def validate_card(card, item):
    if build_card(item) != card:
        raise ValueError('CARD_SOURCE_CHANGED: card or source drift')


def render_card(card):
    return json.dumps({key: field['value'] for key, field in card['fields'].items()},
                      ensure_ascii=False, sort_keys=True)
