'use strict';
let connectionDraft=null;
const connectionSteps=['플랫폼 선택','사이트 주소 입력','공식 연동 승인','게시판 선택','하네스 설정','연결 시험'];
function connectionStep(setup){if(!setup)return 1;if(setup.status==='pending'||setup.status==='denied')return 3;if(!setup.board_id)return 4;if(!setup.rules)return 5;return 6;}
async function renderConnectionWizard(root){
  if(!isAdmin()){root.replaceChildren(empty('관리자만 사이트를 연결할 수 있습니다'));return;}
  const info=await api('/onboarding');
  if(!connectionDraft||connectionDraft.setup?.id!==info.setup?.id||info.setup?.status==='complete'||connectionDraft.setup?.status==='pending'){
    connectionDraft={setup:info.setup,step:connectionStep(info.setup),platform:info.setup?.platform||'',url:info.setup?.site_url||info.current_url||'http://127.0.0.1:8766',rules:info.setup?.rules||info.rules};
  }
  const w=connectionDraft;
  function paint(){
    root.replaceChildren(heading('사이트 연동','쇼핑몰을 연결하고, 문의를 처리할 기준을 설정하세요.',[],'CONNECT YOUR SITE'));
    if(info.connected&&w.setup?.status!=='complete')root.append(notice('기존 사이트가 연결되어 있습니다',`${info.current_url} · 새 연결의 마지막 시험을 통과하면 설정이 적용됩니다.`));
    const layout=el('div','connect-layout'),rail=el('aside','connect-rail'),steps=el('ol','connect-steps');
    connectionSteps.forEach((text,index)=>{const current=index+1===w.step,done=index+1<w.step;const item=el('li',current?'current':done?'done':'');append(item,el('span','step-number',done?'✓':index+1),append(el('div'),el('small','',`STEP ${String(index+1).padStart(2,'0')}`),el('strong','',text)));if(current)item.setAttribute('aria-current','step');steps.append(item);});
    const progress=el('progress');progress.max=6;progress.value=w.step;progress.setAttribute('aria-label',`연결 설정 ${w.step}/6단계`);rail.append(steps,progress,el('p','mini-note','연결 승인 정보는 서버에 보관되며, 브라우저에 API 키를 표시하지 않습니다.'));
    const content=el('section','panel connect-content'),body=el('div','connect-body');content.append(body);layout.append(rail,content);root.append(layout);
    const title=(name,description)=>append(body,el('div','eyebrow',`STEP ${String(w.step).padStart(2,'0')} OF 06`),el('h2','connect-title',name),el('p','connect-description',description));
    const actions=(...buttons)=>append(body,append(el('div','connect-actions'),...buttons));
    const back=()=>button('이전',()=>{state.dirty=false;w.step=Math.max(1,w.step-1);paint();},'');
    const applySetup=setup=>{w.setup=setup;w.step=connectionStep(setup);state.dirty=false;paint();};
    if(w.step===1){
      title('어떤 플랫폼을 사용하시나요?','현재는 MORU 모의 쇼핑몰의 승인과 게시판 연결을 사용할 수 있어요.');
      const cards=el('div','platform-cards');
      for(const platform of info.platforms){const card=el('label',`platform-card${platform.supported?'':' unavailable'}`),radio=el('input');radio.type='radio';radio.name='platform';radio.value=platform.id;radio.disabled=!platform.supported;radio.checked=w.platform===platform.id;radio.onchange=()=>{w.platform=platform.id;paint();};append(card,radio,append(el('div'),append(el('div','platform-name'),el('strong','',platform.name),badge(platform.supported?'active':'inactive',platform.supported?'연결 가능':'준비 중')),el('p','',platform.description)));cards.append(card);}body.append(cards);
      const next=button('사이트 주소 입력 →',()=>{w.step=2;paint();},'primary');next.disabled=!w.platform;actions(next);
    }else if(w.step===2){
      title('사이트 주소를 알려주세요','MORU 고객사이트가 실행되는 주소를 입력해 주세요.');
      const form=el('form');form.append(field('사이트 주소','site_url',w.url,{type:'url',required:true,placeholder:'http://127.0.0.1:8766',hint:'현재 지원 주소: http://127.0.0.1:8766 또는 http://localhost:8766'}),notice('로그인 비밀번호를 입력하지 않습니다','다음 단계에서 MORU 화면을 열어 문의 조회·답글 작성 권한을 승인합니다.'));
      $('[name=site_url]',form).addEventListener('input',event=>{w.url=event.target.value;});
      form.append(append(el('div','connect-actions'),back(),submit('주소 확인하고 계속 →')));onSubmit(form,async values=>{w.url=values.site_url;applySetup(await api('/onboarding/prepare','POST',{platform:w.platform,site_url:w.url}));});body.append(form);
    }else if(w.step===3){
      title('사이트에서 연동을 승인해 주세요','MORU 화면에서 권한을 확인한 뒤 돌아오면 게시판을 선택할 수 있어요.');
      body.append(append(el('div','connect-site-card'),el('strong','','MORU 모의 쇼핑몰'),el('span','',w.setup.site_url)),append(el('ul','permission-list'),...['문의 제목·내용·기존 답변 조회','고객사이트 원본 문의에 답글 작성','새 문의 알림 연결'].map(text=>el('li','',text))),notice('로컬 모의 승인 절차','실제 카페24·아임웹의 OAuth 승인은 아직 제공하지 않습니다. 이 단계는 MORU 고객사이트에서 동작합니다.'));
      if(w.setup.status==='denied'){body.append(notice('연동 승인을 취소했습니다','기존 연결 설정은 유지됩니다. 다시 승인 요청을 보낼 수 있어요.','warning'));actions(back(),button('다시 승인 요청',async()=>applySetup(await api('/onboarding/prepare','POST',{platform:w.platform,site_url:w.url})),'primary'));}
      else{const approve=el('a','button primary','MORU에서 연동 승인 ↗');approve.href=w.setup.approval_url;actions(back(),approve);}
    }else if(w.step===4){
      title('어느 게시판을 연결할까요?','문의 조회와 답글 작성 권한이 확인된 게시판만 선택할 수 있어요.');body.append(notice('연동 승인이 완료되었습니다','연결 토큰은 서버에 안전하게 보관했습니다.','success'));
      const form=el('form'),list=el('div','board-options');for(const board of w.setup.boards||[]){const option=el('label','board-option'),radio=el('input');radio.type='radio';radio.name='board_id';radio.value=board.id;radio.required=true;radio.checked=w.setup.board_id===board.id;radio.disabled=!board.can_read||!board.can_reply;append(option,radio,append(el('div'),el('strong','',board.name),el('small','','문의 읽기 · 답글 작성 가능')));list.append(option);}form.append(list,append(el('div','connect-actions'),submit('게시판 선택하고 계속 →')));onSubmit(form,async values=>applySetup(await api(`/onboarding/${w.setup.id}/board`,'POST',{board_id:values.board_id})));body.append(form);
    }else if(w.step===5){
      title('답변할 때 지킬 기준을 정해주세요','운영자가 정한 규칙은 답변 생성과 게시 전에 적용합니다. 지금 입력한 값은 마지막 연결 시험까지 임시로 보관해요.');
      const rules=w.harnessDraft||w.setup.rules||w.rules||{},lines=value=>(Array.isArray(value)?value:[]).join('\n'),parse=value=>String(value||'').split(/\r?\n/).map(x=>x.trim()).filter(Boolean),form=el('form');
      append(form,field('말투','tone',rules.tone||'',{required:true,maxLength:500,dirty:true,placeholder:'예: 정중하고 간결한 한국어 존댓말'}),field('판단 보류·예외 처리 기준','exception_policy',rules.exception_policy||'',{type:'textarea',required:true,maxLength:500,rows:3,dirty:true,placeholder:'예: 주문 상태·금액·불량 여부를 확인해야 하면 운영자에게 전달'}),field('자동 답변 금지 주제','blocked_topics',lines(rules.blocked_topics),{type:'textarea',rows:3,dirty:true,hint:'한 줄에 하나씩 입력해 주세요. 예: 개별 환불 확정, 불량 판정'}),field('금지 표현','forbidden_phrases',lines(rules.forbidden_phrases),{type:'textarea',rows:2,dirty:true,placeholder:'예: 무조건 보상해 드립니다'}),field('반드시 포함할 안내','required_phrases',lines(rules.required_phrases),{type:'textarea',rows:2,dirty:true,hint:'모든 답변에 꼭 포함해야 하는 문구만 입력해 주세요.'}));
      form.addEventListener('input',()=>{const v=formValues(form);w.harnessDraft={tone:v.tone,exception_policy:v.exception_policy,blocked_topics:parse(v.blocked_topics),forbidden_phrases:parse(v.forbidden_phrases),required_phrases:parse(v.required_phrases)};});
      form.append(append(el('div','connect-actions'),back(),submit('하네스 저장하고 계속 →')));onSubmit(form,async values=>{const result=await api(`/onboarding/${w.setup.id}/rules`,'POST',{rules:{tone:values.tone,exception_policy:values.exception_policy,blocked_topics:parse(values.blocked_topics),forbidden_phrases:parse(values.forbidden_phrases),required_phrases:parse(values.required_phrases)}});w.rules=result.rules;w.harnessDraft=null;applySetup(result);});body.append(form);
    }else{
      const result=w.setup.test_result,complete=w.setup.status==='complete';
      title(complete?'사이트 연결을 마쳤어요':'마지막으로 연결을 시험할게요',complete?'문의 수집과 답글 게시에 사용할 연결·하네스 설정을 적용했습니다.':'문의 조회, 답글 작성, 서명된 문의 알림 수신을 차례로 확인합니다.');
      const chosen=(w.setup.boards||[]).find(b=>b.id===w.setup.board_id);const summary=el('div','connection-summary');summary.append(statusLine('플랫폼','MORU 모의 쇼핑몰'),statusLine('사이트 주소',w.setup.site_url),statusLine('게시판',chosen?.name||w.setup.board_id),statusLine('하네스','입력 완료'));body.append(summary);
      if(result){const checks=el('div','connection-checks');for(const item of result.checks||[])checks.append(append(el('div','connection-check'),icon('check',17),append(el('div'),el('strong','',item.name),el('small','',item.detail)),badge('active','통과')));body.append(checks,notice(result.passed?'연결 시험 통과':'연결 시험을 완료하지 못했습니다',result.message,result.passed?'success':'warning'));}
      body.append(notice('자동 답변은 별도로 활성화합니다','연결 후에도 쇼핑몰의 의미·조건 기반 자동 답변은 별도 품질 검증이 필요합니다.'));
      if(complete)actions(button('새 사이트 연동',()=>{w.rules=w.setup.rules;w.setup=null;w.platform='';w.step=1;info.connected=true;info.current_url=w.url;paint();}),link('문의함으로 이동 →','questions','button primary'));
      else{const run=button('연결 시험 및 설정 적용',async()=>{const pending=notice('연결을 확인하고 있습니다','창을 닫지 말고 잠시 기다려 주세요.');body.append(pending);try{applySetup(await api(`/onboarding/${w.setup.id}/test`,'POST',{}));}finally{pending.remove();}},'primary','link');actions(button('하네스 다시 확인',()=>{w.step=5;paint();}),run);}
    }
  }
  paint();
}
