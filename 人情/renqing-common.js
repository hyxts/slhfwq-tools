// ==================== 礼金系统公共模块 ====================
// 依赖：页面需先定义 const API='/api/renqing'; const $=id=>document.getElementById(id);

// ==================== 工具函数 ====================
function escH(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function fmtD(d){return d||'-';}
function fmtA(v){return (v||0).toLocaleString();}
function evtTypeLabel(t){const m={丧事:'白事'};return m[t]||t;}

let toastTimer;
function toast(msg,isErr){
  const t=$('toast');t.textContent=msg;t.className='toast show'+(isErr?' err':'');
  clearTimeout(toastTimer);toastTimer=setTimeout(()=>t.className='toast',1800);
}

// ==================== 加载状态 ====================
function showLoading(){$('loadingOverlay').classList.add('show');}
function hideLoading(){$('loadingOverlay').classList.remove('show');}

// ==================== 模糊搜索联想 ====================
let acDrops=[];
function acCleanAll(){acDrops.forEach(d=>{if(d.parentNode)d.parentNode.removeChild(d);});acDrops=[];}

function acAttach(input){
  if(input.dataset.acAttached)return;
  input.dataset.acAttached='1';
  const dd=document.createElement('div');dd.className='ac-dd';
  document.body.appendChild(dd);acDrops.push(dd);
  let items=[],idx=-1,timer,blurTimer,ctrl,picking=false;

  function hide(){
    clearTimeout(blurTimer);blurTimer=null;
    dd.classList.remove('show');idx=-1;
  }

  function pick(i){
    if(!items[i])return;
    picking=true;
    input.value=items[i];
    hide();
    input.focus();
    input.dispatchEvent(new Event('input',{bubbles:true}));
    picking=false;
  }

  function render(){
    dd.innerHTML=items.map((n,ii)=>`<div class="ac-item" data-i="${ii}">${escH(n)}</div>`).join('');
    dd.querySelectorAll('.ac-item').forEach(el=>{
      el.addEventListener('mousedown',e=>{e.preventDefault();pick(parseInt(el.dataset.i));});
    });
  }

  function highlight(){
    dd.querySelectorAll('.ac-item').forEach((el,ii)=>el.classList.toggle('active',ii===idx));
  }

  function show(){
    try{
      if(!input.isConnected)return;
      const r=input.getBoundingClientRect();
      dd.style.top=(r.bottom+2)+'px';dd.style.left=r.left+'px';dd.style.minWidth=r.width+'px';
      render();dd.classList.add('show');
    }catch(e){}
  }

  input.addEventListener('input',()=>{
    if(picking)return;
    clearTimeout(timer);clearTimeout(blurTimer);blurTimer=null;idx=-1;
    const q=input.value.trim();
    if(!q){hide();return;}
    if(ctrl){ctrl.abort();}
    ctrl=new AbortController();
    timer=setTimeout(async()=>{
      try{
        const res=await fetch(API+'/suggestions?q='+encodeURIComponent(q),{signal:ctrl.signal});
        items=await res.json();
      }catch(e){return;}
      if(!Array.isArray(items)||items.length===0||document.activeElement!==input||!input.isConnected){hide();return;}
      show();
    },200);
  });

  input.addEventListener('keydown',e=>{
    if(!dd.classList.contains('show'))return;
    if(e.key==='ArrowDown'){e.preventDefault();idx=(idx+1)%items.length;highlight();}
    else if(e.key==='ArrowUp'){e.preventDefault();idx=(idx-1+items.length)%items.length;highlight();}
    else if(e.key==='Enter'||e.key===' '||e.code==='Space'){e.preventDefault();e.stopPropagation();if(idx<0)idx=0;pick(idx);}
    else if(e.key==='Escape'){e.preventDefault();hide();}
  });

  input.addEventListener('blur',()=>{
    clearTimeout(blurTimer);blurTimer=null;
    blurTimer=setTimeout(()=>hide(),200);
  });
}

// ==================== 弹窗 ====================
function showModal(html){
  $('modalBox').innerHTML=html;$('modal').classList.add('show');
  document.body.classList.add('modal-open');
  setTimeout(()=>{
    const firstInp=$('modalBox').querySelector('input,select');
    if(firstInp)firstInp.focus();
  },100);
}
function hideModal(){
  $('modal').classList.remove('show');
  document.body.classList.remove('modal-open');
}

function showConfirm(msg, onOk){
  showModal(`<h3>${msg}</h3>
    <div class="btn-row" style="justify-content:flex-end;margin-top:20px">
      <button class="btn" onclick="hideModal()">取消</button>
      <button class="btn danger" id="confirmOkBtn">确定</button>
    </div>`);
  setTimeout(()=>{
    const btn=$('confirmOkBtn');
    if(btn){btn.addEventListener('click',()=>{hideModal();onOk();},{once:true});}
    btn&&btn.focus();
  },50);
}
