'use strict';
let connectionDraft=null;
const connectionSteps=['플랫폼 선택','사이트 주소 입력','공식 연동 승인','게시판 선택','하네스 설정','연결 시험'];
function connectionStep(setup){if(!setup)return 1;if(setup.status==='pending'||setup.status==='denied')return 3;if(!setup.board_id)return 4;if(!setup.rules)return 5;return 6;}

function connectionHero(title, description, status, action) {
  const body = append(el('div', 'connect-hero-copy'),
    append(el('span', 'connect-hero-icon'), icon('link', 22)),
    el('h1', '', title), el('p', '', description));
  const controls = append(el('div', 'connect-hero-controls'),
    el('span', 'connect-state', status), action);
  return append(el('header', 'connect-hero'), body, controls);
}

async function renderCafe24(root, result) {
  const info = await api('/cafe24');
  root.classList.add('connect-page');
  const status = !info.configured ? '설정 필요' : info.connection ? '승인 완료' : '승인 대기';
  root.replaceChildren(connectionHero('카페24 연동', '쇼핑몰 관리자 승인 후 게시판 목록을 확인할 수 있어요.',
    status, link('사이트 연동으로 돌아가기', 'connect', 'button')));
  const main = el('div', 'cafe-main');
  const layout = append(el('div', 'cafe-layout'), main);
  root.append(layout);
  if (result === 'connected' && info.connection) main.append(notice('카페24 승인을 확인했어요', '조회한 게시판 목록을 아래에서 확인하세요.', 'success'));
  if (result === 'denied') main.append(notice('승인을 취소했어요', '아래에서 다시 승인할 수 있어요.', 'warning'));
  if (result === 'failed') main.append(notice('연결을 확인하지 못했어요', '개발자센터의 콜백 주소, 앱 키, 게시판 읽기 권한을 확인하고 다시 승인하세요.', 'warning'));
  if (!info.configured) {
    const steps = el('ol', 'cafe-prerequisites');
    const requirements = [
      ['문의데스크 HTTPS 주소 준비', '카페24 승인 후 문의데스크로 돌아올 주소를 준비하세요.'],
      ['카페24 개발자 앱 설정', '콜백 주소와 게시판 읽기 권한을 등록하고, 앱 키를 문의데스크 서버에 설정하세요.'],
    ];
    for (const [index, [name, detail]] of requirements.entries()) {
      const number = el('span', 'cafe-step-number', index + 1);
      number.setAttribute('aria-hidden', 'true');
      steps.append(append(el('li'), number, el('strong', '', name), el('p', '', detail)));
    }
    const guide = el('a', 'button', '카페24 개발자 안내 ↗');
    guide.href = 'https://developers.cafe24.com/app/front/app/develop/oauth/put';
    guide.target = '_blank';
    guide.rel = 'noopener noreferrer';
    const setup = append(el('section', 'panel cafe-setup'),
      el('h2', '', '연결 전 준비'),
      el('p', 'cafe-intro', '아래 두 가지를 설정한 뒤 쇼핑몰 관리자 승인을 진행하세요.'),
      steps);
    if (info.redirect_uri) setup.append(append(el('div', 'cafe-callback'),
      el('strong', '', '등록할 콜백 주소'), el('code', '', info.redirect_uri)));
    setup.append(guide);
    main.append(setup);
  }
  const permission = append(el('aside', 'cafe-permission'),
    append(el('span', 'cafe-permission-icon'), icon('shield', 22)),
    el('h2', '', '요청 권한'), el('strong', 'cafe-scope', '게시판 읽기'),
    el('p', '', '승인한 쇼핑몰의 게시판 번호와 이름을 조회합니다.'),
    el('p', 'cafe-scope-note', '문의 수집과 답글 게시는 현재 MORU에서 사용할 수 있어요.'));
  layout.append(permission);
  if (info.configured) {
    const form = el('form', 'panel-body');
    form.append(field('쇼핑몰 아이디', 'mall_id', info.connection?.mall_id || '', {
      required: true, maxLength: 63, placeholder: '예: myshop', hint: '카페24 관리자에서 확인한 쇼핑몰 아이디를 입력하세요.',
    }), submit(info.connection ? '카페24에서 다시 승인' : '카페24에서 승인'));
    onSubmit(form, async values => {
      const {approval_url} = await api('/cafe24/prepare', 'POST', {mall_id: values.mall_id.trim()});
      location.assign(approval_url);
    });
    main.append(append(panel('쇼핑몰 승인'), form));
  }
  if (info.connection) {
    const current = panel('카페24 게시판', `${info.connection.mall_id} · 쇼핑몰 번호 ${info.connection.shop_no}`,
      info.configured ? button('목록 다시 확인', async () => {
        await api('/cafe24/check', 'POST', {});
        await renderCafe24(root);
      }) : null);
    const rows = info.connection.boards.map(board => [board.number, board.name]);
    current.classList.add('cafe-boards');
    current.append(rows.length ? dataTable(['게시판 번호', '게시판 이름'], rows) : empty('조회할 수 있는 게시판이 없어요'));
    main.append(current);
  }
}

async function renderConnectionWizard(root, params){
  if(!isAdmin()){root.replaceChildren(empty('관리자만 사이트를 연결할 수 있습니다'));return;}
  if (params.has('cafe24')) return renderCafe24(root, params.get('cafe24'));
  const info=await api('/onboarding');
  if(!connectionDraft||connectionDraft.setup?.id!==info.setup?.id||info.setup?.status==='complete'||connectionDraft.setup?.status==='pending'){
    connectionDraft={setup:info.setup,step:connectionStep(info.setup),platform:info.setup?.platform||'',url:info.setup?.site_url||info.current_url||'http://127.0.0.1:8766',rules:info.setup?.rules||info.rules};
  }
  const w=connectionDraft;
  root.classList.add('connect-page');
  function paint(){
    const cafe24Link = link('카페24 연동', 'connect?cafe24=setup', 'button');
    root.replaceChildren(connectionHero('사이트 연동', '문의가 들어오는 쇼핑몰과 답변 기준을 관리하세요.',
      info.connected ? 'MORU 연결됨' : '연결 전', cafe24Link));
    if(info.connected&&w.setup?.status!=='complete')root.append(notice('기존 사이트가 연결되어 있습니다',`${info.current_url} · 새 연결의 마지막 시험을 통과하면 설정이 적용됩니다.`));
    const layout=el('div','connect-layout'),rail=el('aside','connect-rail'),steps=el('ol','connect-steps');
    connectionSteps.forEach((text,index)=>{
      const done=index+1<w.step||w.setup?.status==='complete';
      const current=!done&&index+1===w.step;
      const item=el('li',current?'current':done?'done':'');
      const number=el('span','step-number',done?'✓':index+1);
      number.setAttribute('aria-hidden','true');
      append(item,number,el('strong','',text));
      if(current)item.setAttribute('aria-current','step');
      steps.append(item);
    });
    rail.append(el('h2', '', '연결 절차'), steps);
    const content=el('section','panel connect-content'),body=el('div','connect-body');content.append(body);layout.append(rail,content);root.append(layout);
    const title=(name,description)=>append(body,el('h2','connect-title',name),el('p','connect-description',description));
    const actions=(...buttons)=>append(body,append(el('div','connect-actions'),...buttons));
    const back=()=>button('이전',()=>{state.dirty=false;w.step=Math.max(1,w.step-1);paint();},'');
    const applySetup=setup=>{w.setup=setup;w.step=connectionStep(setup);state.dirty=false;paint();};
    if(w.step===1){
      title('어떤 플랫폼을 사용하시나요?','연결할 쇼핑몰을 선택해 주세요.');
      const cards=el('div','platform-cards');
      for(const platform of info.platforms){const card=el('label',`platform-card${platform.supported?'':' unavailable'}`),radio=el('input');radio.type='radio';radio.name='platform';radio.value=platform.id;radio.disabled=!platform.supported;radio.checked=w.platform===platform.id;radio.onchange=()=>{w.platform=platform.id;paint();};append(card,radio,append(el('div'),append(el('div','platform-name'),el('strong','',platform.name),badge(platform.supported?'active':'inactive',platform.supported?'연결 가능':'준비 중')),el('p','',platform.description)));cards.append(card);}body.append(cards);
      const next=button(w.platform==='cafe24'?'카페24 설정':'사이트 주소 입력 →',()=>{
        if (w.platform==='cafe24') { location.hash='connect?cafe24=setup'; return; }
        w.step=2;paint();
      },'primary');next.disabled=!w.platform;actions(next);
    }else if(w.step===2){
      title('사이트 주소를 알려주세요','MORU 고객사이트가 실행되는 주소를 입력해 주세요.');
      const form=el('form');form.append(field('사이트 주소','site_url',w.url,{type:'url',required:true,placeholder:'http://127.0.0.1:8766',hint:'현재 지원 주소: http://127.0.0.1:8766 또는 http://localhost:8766'}),notice('로그인 비밀번호를 입력하지 않습니다','다음 단계에서 MORU 화면을 열어 문의 조회·답글 작성 권한을 승인합니다.'));
      $('[name=site_url]',form).addEventListener('input',event=>{w.url=event.target.value;});
      form.append(append(el('div','connect-actions'),back(),submit('주소 확인하고 계속 →')));onSubmit(form,async values=>{w.url=values.site_url;applySetup(await api('/onboarding/prepare','POST',{platform:w.platform,site_url:w.url}));});body.append(form);
    }else if(w.step===3){
      title('사이트에서 연동을 승인해 주세요','MORU 화면에서 권한을 확인한 뒤 돌아오면 게시판을 선택할 수 있어요.');
      body.append(append(el('div','connect-site-card'),el('strong','','MORU 모의 쇼핑몰'),el('span','',w.setup.site_url)),append(el('ul','permission-list'),...['문의 제목·내용·기존 답변 조회','고객사이트 원본 문의에 답글 작성','새 문의 알림 연결'].map(text=>el('li','',text))),notice('로컬 모의 승인 절차','이 단계는 로컬 MORU 고객사이트에서 진행합니다.'));
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
      if(!complete)body.append(notice('자동 답변은 평가 후 켤 수 있어요','연결을 마친 뒤 운영 설정에서 답변 품질을 평가하세요.'));
      if(complete)actions(button('새 사이트 연동',()=>{w.rules=w.setup.rules;w.setup=null;w.platform='';w.step=1;info.connected=true;info.current_url=w.url;paint();}),link('문의함으로 이동 →','questions','button primary'));
      else{const run=button('연결 시험 및 설정 적용',async()=>{const pending=notice('연결을 확인하고 있습니다','창을 닫지 말고 잠시 기다려 주세요.');body.append(pending);try{applySetup(await api(`/onboarding/${w.setup.id}/test`,'POST',{}));}finally{pending.remove();}},'primary','link');actions(button('하네스 다시 확인',()=>{w.step=5;paint();}),run);}
    }
  }
  paint();
}
