"""API 层共用守卫"""
from fastapi import HTTPException


def reject_null_non_nullable(model, data: dict) -> dict:
    """PUT 显式 null 命中非空列 → 422（原实现未捕获 IntegrityError 直接 500）

    可空列的显式 null 保留（清空语义合法）；只拦会触发约束错误的键。
    """
    cols = model.__table__.columns
    for field, value in data.items():
        col = cols.get(field)
        if value is None and col is not None and not col.nullable:
            raise HTTPException(422, detail={
                "code": "null_not_allowed",
                "message": f"字段 {field} 不能为空"})
    return data
