import hashlib
import uuid
import asyncio
from typing import Optional

from fastapi import APIRouter, Form, UploadFile, File, Depends, HTTPException, BackgroundTasks
from starlette import status

from apps.admin.dependencies import share_required_login
from apps.base.models import FileCodes, UploadChunk
from apps.base.schemas import SelectFileModel, InitChunkUploadModel, CompleteUploadModel
from apps.base.utils import get_expire_info, get_file_path_name, ip_limit, get_chunk_file_path_name
from core.response import APIResponse
from core.settings import settings
from core.storage import storages, FileStorageInterface
from core.utils import get_select_token

share_api = APIRouter(prefix="/share", tags=["分享"])


async def validate_file_size(file: UploadFile, max_size: int):
    """优化的文件大小验证 - 使用流式读取避免全部加载到内存"""
    if hasattr(file, 'size') and file.size and file.size > max_size:
        max_size_mb = max_size / (1024 * 1024)
        raise HTTPException(
            status_code=413, detail=f"文件大小超过限制,最大为{max_size_mb:.2f} MB"
        )


async def create_file_code(code, **kwargs):
    return await FileCodes.create(code=code, **kwargs)


async def process_file_upload_background(file: UploadFile, save_path: str, file_storage):
    """后台异步处理文件上传"""
    try:
        await file_storage.save_file(file, save_path)
    except Exception as e:
        # 记录错误但不阻塞主流程
        from core.logger import logger
        logger.error(f"文件上传后台处理失败: {str(e)}")


@share_api.post("/text/", dependencies=[Depends(share_required_login)])
async def share_text(
        text: str = Form(...),
        expire_value: int = Form(default=1, gt=0),
        expire_style: str = Form(default="day"),
        ip: str = Depends(ip_limit["upload"]),
):
    # 优化文本大小检查
    text_size = len(text.encode("utf-8"))
    max_txt_size = 222 * 1024
    if text_size > max_txt_size:
        raise HTTPException(status_code=413, detail="内容过多,建议采用文件形式")

    expired_at, expired_count, used_count, code = await get_expire_info(
        expire_value, expire_style
    )
    await create_file_code(
        code=code,
        text=text,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        size=len(text),
        prefix="Text",
    )
    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code})


@share_api.post("/file/", dependencies=[Depends(share_required_login)])
async def share_file(
        background_tasks: BackgroundTasks,
        expire_value: int = Form(default=1, gt=0),
        expire_style: str = Form(default="day"),
        file: UploadFile = File(...),
        ip: str = Depends(ip_limit["upload"]),
):
    # 快速验证
    await validate_file_size(file, settings.uploadSize)
    if expire_style not in settings.expireStyle:
        raise HTTPException(status_code=400, detail="过期时间类型错误")
    
    # 并行处理过期信息和文件路径
    expire_task = get_expire_info(expire_value, expire_style)
    path_task = get_file_path_name(file)
    
    expired_at, expired_count, used_count, code = await expire_task
    path, suffix, prefix, uuid_file_name, save_path = await path_task
    
    # 先创建数据库记录，再异步处理文件上传
    await create_file_code(
        code=code,
        prefix=prefix,
        suffix=suffix,
        uuid_file_name=uuid_file_name,
        file_path=path,
        size=file.size or 0,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
    )
    
    # 异步处理文件上传
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    background_tasks.add_task(process_file_upload_background, file, save_path, file_storage)
    
    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": code, "name": file.filename})


async def get_code_file_by_code(code, check=True):
    # 使用select_related优化查询
    file_code = await FileCodes.filter(code=code).first()
    if not file_code:
        return False, "文件不存在"
    if await file_code.is_expired() and check:
        return False, "文件已过期"
    return True, file_code


async def update_file_usage(file_code):
    # 批量更新以提高性能
    file_code.used_count += 1
    if file_code.expired_count > 0:
        file_code.expired_count -= 1
    await file_code.save(update_fields=["used_count", "expired_count"])


@share_api.get("/select/")
async def get_code_file(code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(code)
    if not has:
        ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    # 异步更新使用次数
    asyncio.create_task(update_file_usage(file_code))
    return await file_storage.get_file_response(file_code)


@share_api.post("/select/")
async def select_file(data: SelectFileModel, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    has, file_code = await get_code_file_by_code(data.code)
    if not has:
        ip_limit["error"].add_ip(ip)
        return APIResponse(code=404, detail=file_code)

    # 异步更新使用次数
    asyncio.create_task(update_file_usage(file_code))
    
    # 并行获取文件信息
    text_content = (
        file_code.text
        if file_code.text is not None
        else await file_storage.get_file_url(file_code)
    )
    
    return APIResponse(
        detail={
            "code": file_code.code,
            "name": file_code.prefix + file_code.suffix,
            "size": file_code.size,
            "text": text_content,
        }
    )


@share_api.get("/download")
async def download_file(key: str, code: str, ip: str = Depends(ip_limit["error"])):
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    if await get_select_token(code) != key:
        ip_limit["error"].add_ip(ip)
    has, file_code = await get_code_file_by_code(code, False)
    if not has:
        return APIResponse(code=404, detail="文件不存在")
    return (
        APIResponse(detail=file_code.text)
        if file_code.text
        else await file_storage.get_file_response(file_code)
    )


chunk_api = APIRouter(prefix="/chunk", tags=["切片"])


@chunk_api.post("/upload/init/", dependencies=[Depends(share_required_login)])
async def init_chunk_upload(data: InitChunkUploadModel):
    # # 秒传检查
    # existing = await FileCodes.filter(file_hash=data.file_hash).first()
    # if existing:
    #     if await existing.is_expired():
    #         file_storage: FileStorageInterface = storages[settings.file_storage](
    #         )
    #         await file_storage.delete_file(existing)
    #         await existing.delete()
    #     else:
    #         return APIResponse(detail={
    #             "code": existing.code,
    #             "existed": True,
    #             "name": f'{existing.prefix}{existing.suffix}'
    #         })

    # 创建上传会话
    upload_id = uuid.uuid4().hex
    total_chunks = (data.file_size + data.chunk_size - 1) // data.chunk_size
    await UploadChunk.create(
        upload_id=upload_id,
        chunk_index=-1,
        total_chunks=total_chunks,
        file_size=data.file_size,
        chunk_size=data.chunk_size,
        chunk_hash=data.file_hash,
        file_name=data.file_name,
    )
    # 获取已上传的分片列表
    uploaded_chunks = await UploadChunk.filter(
        upload_id=upload_id,
        completed=True
    ).values_list('chunk_index', flat=True)
    return APIResponse(detail={
        "existed": False,
        "upload_id": upload_id,
        "chunk_size": data.chunk_size,
        "total_chunks": total_chunks,
        "uploaded_chunks": uploaded_chunks
    })


@chunk_api.post("/upload/chunk/{upload_id}/{chunk_index}", dependencies=[Depends(share_required_login)])
async def upload_chunk(
        upload_id: str,
        chunk_index: int,
        chunk: UploadFile = File(...),
):
    # 获取上传会话信息
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    # 检查分片索引有效性
    if chunk_index < 0 or chunk_index >= chunk_info.total_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="无效的分片索引")

    # 读取分片数据并计算哈希
    chunk_data = await chunk.read()
    chunk_hash = hashlib.sha256(chunk_data).hexdigest()

    # 更新或创建分片记录
    await UploadChunk.update_or_create(
        upload_id=upload_id,
        chunk_index=chunk_index,
        defaults={
            'chunk_hash': chunk_hash,
            'completed': True,
            'file_size': chunk_info.file_size,
            'total_chunks': chunk_info.total_chunks,
            'chunk_size': chunk_info.chunk_size,
            'file_name': chunk_info.file_name
        }
    )
    # 获取文件路径
    _, _, _, _, save_path = await get_chunk_file_path_name(chunk_info.file_name, upload_id)
    # 保存分片到存储
    storage = storages[settings.file_storage]()
    await storage.save_chunk(upload_id, chunk_index, chunk_data, chunk_hash, save_path)
    return APIResponse(detail={"chunk_hash": chunk_hash})


@chunk_api.post("/upload/complete/{upload_id}", dependencies=[Depends(share_required_login)])
async def complete_upload(upload_id: str, data: CompleteUploadModel, ip: str = Depends(ip_limit["upload"])):
    # 获取上传基本信息
    chunk_info = await UploadChunk.filter(upload_id=upload_id, chunk_index=-1).first()
    if not chunk_info:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="上传会话不存在")

    storage = storages[settings.file_storage]()
    # 验证所有分片
    completed_chunks = await UploadChunk.filter(
        upload_id=upload_id,
        completed=True
    ).count()
    if completed_chunks != chunk_info.total_chunks:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="分片不完整")
    # 获取文件路径
    path, suffix, prefix, _, save_path = await get_chunk_file_path_name(chunk_info.file_name, upload_id)
    # 合并文件并计算哈希
    await storage.merge_chunks(upload_id, chunk_info, save_path)
    # 创建文件记录
    expired_at, expired_count, used_count, code = await get_expire_info(data.expire_value, data.expire_style)
    await FileCodes.create(
        code=code,
        file_hash=chunk_info.chunk_hash,
        is_chunked=True,
        upload_id=upload_id,
        size=chunk_info.file_size,
        expired_at=expired_at,
        expired_count=expired_count,
        used_count=used_count,
        file_path=path,
        uuid_file_name=f"{prefix}{suffix}",
        prefix=prefix,
        suffix=suffix
    )
    # 清理临时文件
    await storage.clean_chunks(upload_id, save_path)
    return APIResponse(detail={"code": code, "name": chunk_info.file_name})
