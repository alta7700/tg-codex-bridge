"""Pure transport metadata; quoted text is never interpreted as instructions."""
from __future__ import annotations

import json


def author_fields(message):
    user = message.get('from') or message.get('from_user') or {}
    chat = message.get('sender_chat') or {}
    if chat:
        username = chat.get('username')
        name = chat.get('title') or chat.get('first_name') or username
        author = {'type':'chat','chat_id':chat.get('id'),'name':name,'username':username,
                  'mention':'@'+username if username else None,'signature':message.get('author_signature')}
        return {'user_id':None,'sender':name,'username':username,'mention':author['mention'],'author':author}
    # Locally stored outgoing/incoming events already contain normalized metadata.
    if not user and ('sender' in message or 'author' in message):
        username = message.get('username')
        author = message.get('author') or {'type':'user','user_id':message.get('user_id'),
            'name':message.get('sender'),'username':username,'is_bot':message.get('is_bot')}
        return {'user_id':message.get('user_id'),'sender':message.get('sender'),
                'username':username,'mention':'@'+username if username else None,'author':author}
    username = user.get('username')
    name = ' '.join(filter(None,[user.get('first_name'),user.get('last_name')])) or username or None
    author = {'type':'user','user_id':user.get('id'),'name':name,'username':username,
              'mention':'@'+username if username else None,'is_bot':user.get('is_bot')}
    return {'user_id':user.get('id'),'sender':name,'username':username,'mention':author['mention'],'author':author}


def quoted_message(message,source):
    # One level only: don't expand a whole conversation or redownload quoted files.
    attachments = message.get('attachments')
    if attachments is None:
        attachments = [{'type':kind} for kind in ('photo','document','voice','audio','video','sticker') if message.get(kind)]
    return {'chat_id':(message.get('chat') or {}).get('id',message.get('chat_id')),
            'message_id':message['message_id'],'date':message.get('date'),
            **author_fields(message),'text':message.get('text') or message.get('caption') or '',
            'attachments':attachments,'source':source,'available':True}


def message_event(message):
    parent = message.get('reply_to_message')
    event = {'source':'telegram','chat_id':message['chat']['id'],'message_id':message['message_id'],
             **author_fields(message),'date':message.get('date'),
             'text':message.get('text') or message.get('caption') or '',
             'reply_to_message_id':parent['message_id'] if parent else None,
             'media_group_id':message.get('media_group_id'),'attachments':[]}
    if parent:
        event['reply_to'] = quoted_message(parent,'telegram_reply')
    if message.get('quote'):
        event['quote'] = {'text':message['quote'].get('text'),'position':message['quote'].get('position')}
    return event


def event_from_raw(raw):
    return message_event(json.loads(raw))
