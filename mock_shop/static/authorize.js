'use strict';
const approvalId=new URL(location.href).searchParams.get('request_id');
const approvalError=document.querySelector('#approval-error');
async function authorizationApi(path,data){const response=await fetch(path,data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}:{});const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'승인 요청을 확인해 주세요.');return result;}
document.querySelector('#consent').addEventListener('change',event=>{document.querySelector('#approve').disabled=!event.target.checked;});
async function decide(accepted){document.querySelector('#approve').disabled=true;document.querySelector('#deny').disabled=true;try{const result=await authorizationApi('/api/authorizations/'+encodeURIComponent(approvalId)+'/decision',{accepted});location.assign(result.redirect_url);}catch(error){approvalError.textContent=error.message;document.querySelector('#deny').disabled=false;document.querySelector('#approve').disabled=!document.querySelector('#consent').checked;}}
document.querySelector('#approve').addEventListener('click',()=>decide(true));
document.querySelector('#deny').addEventListener('click',()=>decide(false));
(async()=>{try{const info=await authorizationApi('/api/authorizations/'+encodeURIComponent(approvalId));if(info.status!=='pending')throw new Error('이미 처리된 요청입니다. 문의데스크에서 새 연결을 시작해 주세요.');const list=document.querySelector('#permissions');list.replaceChildren();for(const permission of info.permissions){const item=document.createElement('li');item.textContent=permission;list.append(item);}}catch(error){approvalError.textContent=error.message;document.querySelector('#consent').disabled=true;document.querySelector('#deny').disabled=true;}})();
