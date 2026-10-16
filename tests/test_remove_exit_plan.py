import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.autoexit_service import create_exit_router
from app.kiwoom.orders import NotSent
from tests.test_autoexit import rig, arm


async def inactive(r):
    p = await arm(r)
    r.e.pause(p['code'])
    return p


def test_delete_is_persistent_keeps_other_active_plan_and_orders(rig):
    async def run():
        p = await inactive(rig)
        other = dict(p, id='other', code='126340', name='다른 종목', status='armed')
        rig.e.plans['126340'] = other
        rig.e.orders.append({'code':p['code'], 'status':'filled', 'id':'history'})
        rig.e.quotes[p['code']] = (10000, rig.now[0])
        rig.e.quotes['126340'] = (10000, rig.now[0])
        rig.e.persist()
        original_other = copy.deepcopy(other)
        epoch = rig.e.epoch
        rig.connected[0] = False  # Removing an inactive plan needs no broker connection.
        await rig.e.remove_plan(p['code'], p['id'])
        assert p['code'] not in rig.e.plans
        assert rig.e.plans['126340'] == original_other and rig.e.epoch == epoch
        assert p['code'] not in rig.e.quotes and '126340' in rig.e.quotes
        saved = json.loads(rig.e.path.read_text('utf-8'))
        assert p['code'] not in saved['plans']
        assert saved['plans']['126340'] == original_other
        assert saved['orders'] == rig.e.orders == [{'code':p['code'], 'status':'filled', 'id':'history'}]
        assert not rig.b.calls
        rig.e.close_lock(); rig.e.open()
        assert p['code'] not in rig.e.plans
    asyncio.run(run())


@pytest.mark.parametrize('state', ['prepared','armed','checking','ordered','remainder_check'])
def test_active_or_transitional_plan_cannot_be_deleted(rig,state):
    async def run():
        p=await inactive(rig);p['status']=state
        with pytest.raises(NotSent):await rig.e.remove_plan(p['code'],p['id'])
        assert p['code'] in rig.e.plans and p['status']==state and not rig.b.calls
    asyncio.run(run())


@pytest.mark.parametrize('status', ['submitting','accepted','partial','unknown','attention'])
def test_unresolved_orders_block_removal_even_for_paused_plan(rig,status):
    async def run():
        p=await inactive(rig)
        rig.e.orders.append({'code':p['code'],'status':status})
        with pytest.raises(NotSent):await rig.e.remove_plan(p['code'],p['id'])
        assert p['code'] in rig.e.plans and not rig.b.calls
    asyncio.run(run())


def test_stale_identity_does_not_remove_replaced_plan(rig):
    async def run():
        p=await inactive(rig);old=p['id'];p['id']='replacement'
        with pytest.raises(NotSent):await rig.e.remove_plan(p['code'],old)
        assert rig.e.plans[p['code']]['id']=='replacement'
    asyncio.run(run())


def test_failed_save_restores_plan_and_never_reports_success(rig,monkeypatch):
    async def run():
        p=await inactive(rig)
        def fail():raise OSError('simulated disk failure')
        monkeypatch.setattr(rig.e,'save',fail)
        with pytest.raises(OSError):await rig.e.remove_plan(p['code'],p['id'])
        assert rig.e.plans[p['code']] is p and rig.e.error
        assert p['code'] in json.loads(rig.e.path.read_text('utf-8'))['plans']
        assert not rig.b.calls
    asyncio.run(run())


def test_removal_waits_for_reconciliation(rig):
    async def run():
        p=await inactive(rig)
        await rig.e.reconcile_lock.acquire()
        task=asyncio.create_task(rig.e.remove_plan(p['code'],p['id']))
        await asyncio.sleep(0)
        assert not task.done() and p['code'] in rig.e.plans
        rig.e.reconcile_lock.release();await task
        assert p['code'] not in rig.e.plans
    asyncio.run(run())


def test_delete_endpoint_requires_token_origin_and_exact_identity(rig):
    async def run():
        p=await inactive(rig)
        app=FastAPI();app.include_router(create_exit_router(SimpleNamespace(engine=rig.e,token='fake')))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
            body={'code':p['code'],'plan_id':p['id']}
            endpoint='/api/autoexit/remove'
            assert (await client.post(endpoint,json=body)).status_code==403
            headers={'X-Autoexit-Token':'fake','Origin':'https://wrong.example'}
            assert (await client.post(endpoint,json=body,headers=headers)).status_code==403
            headers['Origin']='http://127.0.0.1:8000'
            assert (await client.post(endpoint,json=body|{'plan_id':'wrong'},headers=headers)).status_code==409
            assert (await client.post(endpoint,json=body|{'unexpected':True},headers=headers)).status_code==422
            response=await client.post(endpoint,json=body,headers=headers)
            assert response.status_code==200 and response.json()['deleted_plan_id']==p['id']
            assert response.json()['plans']==[] and not rig.b.calls
    asyncio.run(run())
