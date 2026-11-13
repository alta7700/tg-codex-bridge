from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramNetworkError, TelegramServerError
from aiogram.types import BufferedInputFile, ReplyParameters, Message
from .events import message_event, author_fields

MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 49 * 1024 * 1024
EXPORT_TYPES = {'.pdf','.docx','.xlsx','.csv','.tsv','.txt','.md','.png','.jpg','.jpeg','.webp','.mp3','.wav','.ogg'}


class DownloadRejected(ValueError):
    pass


def chunks(text, limit=4000):
    part, size = [], 0
    for char in text:
        length = len(char.encode('utf-16-le')) // 2
        if size+length > limit:
            yield ''.join(part)
            part, size = [], 0
        part.append(char);size += length
    if part:
        yield ''.join(part)


def export_bytes(workspace: Path, path: str, denied_roots=()):
    """Workspace-relative path, no traversal, symlinks, hardlinks or private trees."""
    requested = Path(path)
    if not path or requested.is_absolute() or '..' in requested.parts:
        raise ValueError('File must be relative to workspace, without ..')
    if not requested.parts or any(p.startswith('.') for p in requested.parts):
        raise ValueError('Hidden files are not exportable')
    candidate = workspace / requested
    for denied in denied_roots:
        if candidate.is_relative_to(denied):
            raise ValueError('Private bridge files are not exportable')
    if requested.suffix.lower() not in EXPORT_TYPES:
        raise ValueError('Unsupported export file type')
    if any(any(word in part.lower() for word in ('secret','token','credential','password','id_rsa')) for part in requested.parts):
        raise ValueError('Potential secret filename')
    fd = os.open(workspace,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in requested.parts[:-1]:
            nxt = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd = nxt
        file_fd = os.open(requested.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        with os.fdopen(file_fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_UPLOAD:
                raise ValueError('Export must be a regular non-hardlinked file below 49 MiB')
            data = stream.read(MAX_UPLOAD+1)
            if len(data)>MAX_UPLOAD:
                raise ValueError('Export exceeds upload limit')
            if b'-----BEGIN PRIVATE KEY-----' in data or b'-----BEGIN OPENSSH PRIVATE KEY-----' in data:
                raise ValueError('Private keys cannot be exported')
            return data, requested.name
    finally:
        os.close(fd)


async def download_event(bot, cfg, raw):
    message = Message.model_validate_json(raw)
    # Validation restores SDK defaults; use the stored JSON for metadata rather
    # than serializing those defaults again (notably inside reply previews).
    event = message_event(json.loads(raw))
    media = []
    if message.photo:
        media.append(('photo',message.photo[-1],'.jpg'))
    for kind,suffix in [('document','.bin'),('voice','.ogg'),('audio','.audio'),('video','.mp4')]:
        value = getattr(message,kind)
        if value:
            media.append((kind,value,suffix))
    directory = cfg.attachments / str(message.message_id)
    if directory.is_symlink():
        raise DownloadRejected('Unsafe message attachment directory')
    if media:
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    for index,(kind,value,suffix) in enumerate(media):
        original = getattr(value,'file_name',None)
        ext = Path(original).suffix if original else suffix
        if not ext or len(ext)>12 or not all(c.isalnum() or c=='.' for c in ext):
            ext = suffix
        target = directory / f'{message.message_id}_{index}{ext}'
        if value.file_size and value.file_size>MAX_DOWNLOAD:
            raise DownloadRejected('Attachment exceeds Telegram download limit (20 MiB)')
        if target.is_symlink():
            raise DownloadRejected('Unsafe attachment path')
        if not target.exists():
            tmp = target.with_suffix(target.suffix+'.part')
            if tmp.is_symlink():
                raise DownloadRejected('Unsafe partial download path')
            tmp.unlink(missing_ok=True)
            file = await bot.get_file(value.file_id,request_timeout=30)
            await bot.download_file(file.file_path,destination=tmp,timeout=60)
            if tmp.stat().st_size>MAX_DOWNLOAD:
                tmp.unlink()
                raise DownloadRejected('Attachment exceeds download limit')
            with tmp.open('rb') as downloaded:
                os.fsync(downloaded.fileno())
            tmp.chmod(0o400)
            tmp.replace(target)
        event['attachments'].append({'type':kind,'message_id':message.message_id,'filename':original,
            'mime_type':getattr(value,'mime_type',None),'path':str(target),
            'relative_path':str(target.relative_to(cfg.workspace))})
    return event


def codex_input(event, marker, reminder=False):
    prefix = 'напоминаю, если забыл\n' if reminder else ''
    result = [{'type':'text','text':prefix+marker+'\n'+json.dumps(event,ensure_ascii=False)}]
    for attachment in event['attachments']:
        if attachment.get('path') and attachment['type']=='photo':
            result.append({'type':'localImage','path':attachment['path']})
    return result


class Sender:
    def __init__(self,cfg,store,bot: Bot):
        self.cfg,self.store,self.bot = cfg,store,bot
        self.lock = asyncio.Lock()
        self.pause = asyncio.sleep

    async def deliver(self,key,payload):
        if payload.get('chat_id') != self.cfg.chat_id:
            raise ValueError('Destination chat is not allowed')
        if payload.get('op') not in {'text','file'}:
            raise ValueError('Unsupported operation')
        async with self.lock:
            row = self.store.enqueue_send(key,payload)
            if row['status']=='sent':
                return json.loads(row['result'])
            if row['status'] in {'uncertain','failed','sending'}:
                raise ValueError(f"Delivery {row['status']}; inspect tg-bridge status before retry")
            try:
                reply_to = payload.get('reply_to')
                if reply_to is not None and (not isinstance(reply_to,int) or reply_to<=0):
                    raise ValueError('reply_to must be a positive message_id')
                # A successful standalone fallback must never count as a reply.
                reply = ReplyParameters(message_id=reply_to,allow_sending_without_reply=False) if reply_to else None
                common = {'chat_id':self.cfg.chat_id,'reply_parameters':reply}
                if payload['op']=='text':
                    if not isinstance(payload.get('text'),str):
                        raise ValueError('text must be a string')
                    pieces = list(chunks(payload['text']))
                    if not pieces or len(pieces)>100:
                        raise ValueError('Empty or excessive text')
                    calls = [(self.bot.send_message,{**common,'text':part},part,[]) for part in pieces]
                else:
                    if not isinstance(payload.get('file'),str):
                        raise ValueError('file must be a workspace-relative path')
                    data,name = export_bytes(self.cfg.workspace,payload['file'],(self.cfg.root,))
                    attachment = {'type':'document','relative_path':payload['file'],'filename':name}
                    calls = [(self.bot.send_document,{**common,'document':BufferedInputFile(data,filename=name)},'', [attachment])]
            except Exception as error:
                self.store.delivery(key,'failed',error=type(error).__name__)
                raise
            self.store.delivery(key,'sending')
            sent = []
            try:
                for index,(fn,kwargs,text,attachments) in enumerate(calls):
                    while True:
                        try:
                            result = await fn(**kwargs)
                            sent.append(result.message_id)
                            user = getattr(result,'from_user',None)
                            sender = author_fields({'from':user.model_dump(mode='json')}) if user is not None else None
                            self.store.record_fragment(key,index,result.message_id,payload,text,attachments,sender)
                            await self.pause(1.05)
                            break
                        except TelegramRetryAfter as error:
                            await self.pause(error.retry_after+0.2)
                self.store.delivery(key,'sent',sent)
                return sent
            except (TelegramNetworkError,TelegramServerError,asyncio.CancelledError):
                self.store.delivery(key,'uncertain',sent,'Telegram delivery acknowledgement missing')
                raise
            except Exception as error:
                self.store.delivery(key,'uncertain' if sent else 'failed',sent,type(error).__name__)
                raise
