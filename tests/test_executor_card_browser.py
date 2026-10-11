"""Actual chat renderers over deterministic progress/history, without model calls."""
from __future__ import annotations
import json
import pytest
from tests.test_subscription_setup_browser import subscription_ui as subscription_ui, capture

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def progress(task, text, **extra):
    return dict(role='assistant', direction='out', is_progress=True, task_id=task,
                text=text, content=text, ts='2026-09-09T10:00:00Z', **extra)


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('linux_metrics', [False, True], ids=['native-font', 'linux-metrics'])
def test_observed_executor_role_activity_and_project_pointer_live_replay(subscription_ui, width, linux_metrics):
    ui = subscription_ui
    page = ui['page']
    page.set_viewport_size({'width':width,'height':800})
    observation = dict(task_id='child',task_attempt='1',run_id='run-a',attempt_id='a01',
                       harness_id='cursor',phase='harness.event',revision=8,
                       model='cursor-grok-4.6-xhigh-fast',model_source='requested')
    child = progress('child','Checking every visible control and its alignment.',
                     subagent_event='running',subagent_task_id='child',parent_task_id='root',
                     root_task_id='root',delegation_role='subagent',subagent_role='UI reviewer',
                     model='openai/gpt-6-astra',executor_route='cursor',executor_observation=observation)
    rows = [progress('root','Inspecting the shared UI system. '+ 'Useful activity text. '*20,suggested_name='UI coherence', model='openai/gpt-6-astra'), child]
    history = {'messages': rows, 'progress': [], 'page_cursor': 'executor-recent',
               'next_cursor': 'executor-older', 'has_more': True,
               'window': {'complete': False, 'truncated_by': ['quota']},
               'coverage': {'v': 1, 'view': 'executor-proof', 'upper': {'chat': 0, 'progress': 7},
                            'spans': {'chat': {'from': 0, 'to': 0, 'chain': 'empty', 'gaps': []},
                                      'progress': {'from': 5, 'to': 7, 'chain': 'retained', 'gaps': ['incomplete_live_line']}}}}
    page.route('**/api/chat/history*',lambda route:route.fulfill(content_type='application/json',body=json.dumps(history)))
    page.goto(ui['url'])
    page.wait_for_selector('#chat-input')
    # A real secondary instance with its own mock WS boundary and the same history API.
    page.evaluate('''async () => {
        const {createChatInstance} = await import('/static/modules/chat.js');
        const {createStateSnapshotSequencer} = await import('/static/modules/chat_activity.js');
        const mount=document.createElement('aside'); mount.id='executor-proof';
        mount.className='project-panel open'; document.body.append(mount);
        const handlers=new Map(); const ws={on(type,fn){
            if(!handlers.has(type))handlers.set(type,new Set());handlers.get(type).add(fn);
            return()=>handlers.get(type).delete(fn);},isConnected:()=>true,send:()=>{}};
        window.executorProof={handlers,instance:createChatInstance({ws,state:{activePage:'chat',projectChatIds:new Set()},
            updateUnreadBadge:()=>{},stateSnapshots:createStateSnapshotSequencer(()=>{}),chatId:101,projectId:'proof',idPrefix:'proof',mountEl:mount,asPanel:true})};
        await executorProof.instance.refreshHistory();
    }''')
    card = page.locator('#executor-proof [data-task-id="child"]')
    card.wait_for()
    root_card = page.locator('#executor-proof [data-task-id="root"]')
    root_meta = root_card.locator(':scope > .chat-live-summary-button > .chat-live-meta')
    assert 'Agent model: gpt-6-astra' in root_meta.inner_text()
    # A helper's usage shares the task id but must not replace its coordinator.
    page.evaluate('''async () => {
        for (const fn of executorProof.handlers.get('log') || []) fn({chat_id:101, data:{
            type:'llm_usage', task_id:'root', model:'helper-model', model_category:'websearch',
            ts:'2026-09-09T10:00:30Z'}});
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }''')
    assert 'Agent model: gpt-6-astra' in root_meta.inner_text()
    assert 'helper-model' not in root_meta.inner_text()
    assert card.locator('.chat-live-title').first.inner_text() == 'UI reviewer'
    assert 'Coordinator: gpt-6-astra' in card.inner_text()
    assert 'Cursor' in card.locator('.chat-live-executor-chip').inner_text()
    assert '(requested)' in card.locator('.chat-live-executor-chip').inner_text()
    activity=card.locator('.chat-live-activity').first
    assert activity.is_visible() and activity.inner_text().startswith('Checking every')
    assert activity.evaluate('e=>getComputedStyle(e).webkitLineClamp') == '1'
    pointer=page.locator('#executor-proof .project-work-pointer')
    assert 'UI coherence' in pointer.inner_text()
    if linux_metrics:
        # Exercise the Linux status-pill width without bundling another font.
        page.add_style_tag(content='''
            #executor-proof .chat-panel-statusbar .status-badge { min-width: 81px; }
        ''')
    # One-line pointer: the label ellipsizes instead of wrapping the status bar open.
    label=pointer.locator('.project-work-pointer-label')
    assert label.evaluate('e=>[getComputedStyle(e).whiteSpace,getComputedStyle(e).textOverflow]')==['nowrap','ellipsis']
    assert pointer.bounding_box()['height'] <= 40
    # Pointer and pill share one row. The common history disclosure has its own
    # readable, wrapping row; its content determines the additional bar height.
    assert label.evaluate('e=>e.scrollWidth<=e.clientWidth')
    bar = page.locator('#executor-proof .chat-panel-statusbar')
    note = bar.locator('.chat-history-status')
    assert note.inner_text() == 'Some saved history is not loaded. Shown messages may have gaps.'
    assert page.locator('#executor-proof .chat-load-older-note').count() == 1
    assert page.locator('#executor-proof .project-work-coverage').count() == 0
    geometry = bar.evaluate('''bar => {
        const box = selector => bar.querySelector(selector).getBoundingClientRect();
        const pointer = box('.project-work-pointer'), pill = box('.status-badge'), note = box('.chat-history-status');
        const css = getComputedStyle(bar), rect = bar.getBoundingClientRect();
        return {pointer, pill, note, bar:rect,
            spacing:parseFloat(css.paddingTop)+parseFloat(css.paddingBottom)+parseFloat(css.rowGap || 0)};
    }''')
    assert abs(geometry['pointer']['y'] - geometry['pill']['y']) <= 8
    assert geometry['note']['y'] >= max(geometry['pointer']['bottom'], geometry['pill']['bottom'])
    assert geometry['note']['x'] >= geometry['bar']['x']
    assert geometry['note']['right'] <= geometry['bar']['right'] + 1
    assert geometry['bar']['height'] <= max(geometry['pointer']['height'], geometry['pill']['height']) + geometry['note']['height'] + geometry['spacing'] + 3
    input_box=page.locator('#proof-input')
    input_box.fill('Message stays in this Project')
    pointer.click()
    assert input_box.input_value() == 'Message stays in this Project'
    terminal={**child,'is_progress':True,'subagent_event':'completed','status':'completed','result':'Checks complete',
              'ts':'2026-09-09T10:01:00Z','execution_evidence':{'delegated_runs_started':1,'delegated_runs_settled':1,
              'delegated_runs_failed':0,'harness_models':['Cursor Grok 4.6 Extra High Fast']}}
    page.evaluate('''frame=>{for(const fn of executorProof.handlers.get('chat')||[])fn({...frame,chat_id:101})}''',terminal)
    page.wait_for_function("document.querySelector('#executor-proof [data-task-id=child]').textContent.includes('Observed: Cursor Grok 4.6')")
    assert 'Coordinator: gpt-6-astra' in card.inner_text()
    capture(page,f'executor-card-{width}-{"linux-metrics" if linux_metrics else "native-font"}')
    page.evaluate('executorProof.instance.destroy()')
    assert page.locator('#executor-proof .project-work-pointer').count()==0
