'use strict';
async function renderReferenceDrafts(root){
  let info=await api('/reference-drafts');
  if(!info.available)return;
  const box=panel('FAQ·기존 답변으로 초안 준비','공개 FAQ와 승인된 답변을 근거로 작성하며, 고객사이트에 자동 게시하지 않습니다.');
  const body=el('div','panel-body');box.append(body);root.append(box);
  function draw(){
    const job=info.job,running=job.status==='running';body.replaceChildren();
    body.append(el('p','mini-note',`작성하지 않은 MORU 문의 ${info.pending}건 · 기존 초안과 결론·이유는 유지합니다. 저장된 OpenAI 키를 사용하며 AI 사용료가 발생합니다.`));
    if(job.status!=='idle'){
      body.append(notice(running?'초안을 준비하고 있습니다':job.status==='failed'?'초안 준비 중단':'초안 준비 완료',`이번 실행 ${job.processed}/${job.total}건 확인 · 신규 초안 ${job.created}건 · 근거 부족 보류 ${job.held}건 · 건너뜀 ${job.skipped}건. ${job.message}`,job.status==='failed'?'warning':running?'':'success'));
      if(running){const progress=el('progress');progress.max=job.total||1;progress.value=job.processed;progress.setAttribute('aria-label',`초안 준비 ${job.processed}/${job.total}건`);progress.style.width='100%';body.append(progress);}
    }
    if(!info.ai_configured)body.append(notice('OpenAI 키가 필요합니다','운영 설정에서 API 키를 저장하면 근거 기반 초안을 준비할 수 있습니다.'));
    const actions=el('div','actions');
    if(isAdmin()){
      const start=button('미답변 초안 일괄 준비',async()=>{
        if(state.dirty)throw new Error('작성 중인 답변을 먼저 저장해 주세요.');
        if(!await confirmAction('근거 기반 초안을 준비할까요?','MORU 공개 FAQ를 초안용 지식으로 가져옵니다. 자동 답변 사용 권한은 부여하지 않습니다. 미작성 문의만 AI로 초안을 만들며, 기존 월 예산 제한을 적용합니다.','초안 준비'))return;
        await api('/reference-drafts','POST',{});info=await api('/reference-drafts');draw();poll();
      },'primary','spark');
      start.disabled=running||!info.ai_configured||info.pending===0;actions.append(start);
    }
    actions.append(button('준비된 초안 보기 →',async()=>{
      if(state.dirty)throw new Error('작성 중인 답변을 먼저 저장해 주세요.');
      const target='#questions?status=draft';
      if(location.hash===target)await renderRoute(true);else location.hash=target;
    }));body.append(actions);
  }
  async function poll(){
    if(info.job.status!=='running')return;
    setTimeout(async()=>{
      if(!box.isConnected)return;
      try{info=await api('/reference-drafts');draw();poll();}catch(error){body.append(notice('진행 상황을 확인하지 못했습니다',error.message,'warning'));}
    },2500);
  }
  draw();poll();
}
