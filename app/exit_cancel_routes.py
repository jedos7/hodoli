from fastapi import Depends,HTTPException
from pydantic import BaseModel,ConfigDict,StrictBool
from app.kiwoom.orders import NotSent
class CancelConfirm(BaseModel):
    model_config=ConfigDict(extra='forbid')
    review_id:str
    environment:str
    confirm:StrictBool

def attach_cancel_routes(router,service,mutate):
    @router.post('/cancel/prepare',dependencies=[Depends(mutate)])
    async def prepare_cancel():
        try:return await service.engine.prepare_manual_cancel()
        except NotSent as exc:raise HTTPException(409,str(exc)) from exc
        except Exception as exc:raise HTTPException(409,'취소 대상 조회 실패 · 전송하지 않았습니다') from exc
    @router.post('/cancel/confirm',dependencies=[Depends(mutate)])
    async def confirm_cancel(body:CancelConfirm):
        try:return await service.engine.confirm_manual_cancel(**body.model_dump())
        except NotSent as exc:raise HTTPException(409,str(exc)) from exc
        except Exception as exc:raise HTTPException(409,'취소 결과 확인 필요 · 자동 재전송하지 않습니다') from exc
