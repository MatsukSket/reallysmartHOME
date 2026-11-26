var DN=['Пн','Вт','Ср','Чт','Пт','Сб','Вс'];
var ROOMS=['Гостиная','Спальня','Котельная','Детская','Кухня','Ванная','Прихожая'];
var TYPES={
 ac:{t:'Кондиционер',s:{l:'Температура',v:22,min:16,max:30,u:'°C'}},
 boiler:{t:'Котел',s:{l:'Температура воды',v:65,min:30,max:85,u:'°C'}},
 lamp:{t:'Умная лампа',s:{l:'Яркость',v:60,min:0,max:100,u:'%'}},
 humid:{t:'Увлажнитель',s:{l:'Целевая влажность',v:50,min:30,max:70,u:'%'}},
 vacuum:{t:'Робот-пылесос'},
 sensor:{t:'Датчик',s:{l:'Чувствительность',v:50,min:0,max:100,u:'%'}},
 plug:{t:'Розетка',s:{l:'Лимит мощности',v:1500,min:500,max:3000,u:' Вт'}},
 cam:{t:'Камера',s:{l:'Чувствительность',v:70,min:0,max:100,u:'%'}},
 other:{t:'Устройство'}
};
var DEV=[
 {name:'Кондиционер',room:'Гостиная',type:'ac',on:1,val:22},
 {name:'Увлажнитель',room:'Спальня',type:'humid',on:1,val:50,mode:'Авто'},
 {name:'Робот-пылесос',room:'Гостиная',type:'vacuum',on:0,val:0,sched:{mode:'cal',time:'10:00',days:[0,1,2,3,4],timer:30}},
 {name:'Котел',room:'Котельная',type:'boiler',on:1,val:65},
 {name:'Умная лампа',room:'Спальня',type:'lamp',on:0,val:60}
];
var PASS='admin',isAdmin=false,USERS=[{n:'Максим',r:'Пользователь'},{n:'Лиза',r:'Пользователь'}];
var curRoom=0,cur=0,addType='ac';

function $(id){return document.getElementById(id)}
function ico(n){return '<svg class="ico"><use href="#i-'+n+'"/></svg>'}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function schedText(d){
  var s=d.sched; if(s.mode==='timer')return 'через '+s.timer+' мин';
  if(!s.days.length)return 'без расписания';
  return (s.days.length===7?'каждый день':s.days.slice().sort().map(function(i){return DN[i]}).join(', '))+' в '+s.time;
}

function devsOf(r){return DEV.map(function(d,i){return {d:d,i:i}}).filter(function(x){return x.d.room===r})}
function renderList(){
  var r=ROOMS[curRoom],ds=devsOf(r),h='';
  $('room-title').textContent=r;
  $('room-sub').textContent=ds.length?ds.length+' '+(ds.length===1?'устройство':ds.length<5?'устройства':'устройств'):'Устройств пока нет';
  ds.forEach(function(x){
    var d=x.d,sub=d.type==='vacuum'?(d.on?'Убирает':'На базе')+' · '+schedText(d):TYPES[d.type].t;
    h+='<button data-dev="'+x.i+'" class="glass hov w-full p-4 flex items-center gap-4 text-left"><span class="ibox" style="background:'+(d.on?'var(--accsoft)':'var(--soft)')+';color:'+(d.on?'var(--acc)':'var(--mute)')+'">'+ico(d.type)+'</span><span class="flex-1 min-w-0"><span class="block font-semibold">'+esc(d.name)+'</span><span class="block mute text-xs mt-0.5 truncate">'+sub+'</span></span>'+
      (d.on?'<span class="chip" style="background:var(--oksoft);color:var(--ok)">Вкл</span>':'<span class="chip" style="background:var(--soft);color:var(--mute)">Выкл</span>')+'</button>';
  });
  $('dev-list').innerHTML=h||'<div class="glass p-5 text-sm mute text-center">Добавьте первое устройство в эту комнату</div>';
  $('cnt-on').textContent=DEV.filter(function(d){return d.on}).length; $('cnt-all').textContent=DEV.length;
  renderMap(); renderHome();
}

function renderMap(){
  var h='';
  ROOMS.forEach(function(r,k){
    var ds=devsOf(r),any=ds.some(function(x){return x.d.on});
    h+='<button data-room="'+k+'" class="hov rounded-2xl p-3 flex flex-col justify-between text-left'+(k===ROOMS.length-1&&ROOMS.length%2?' col-span-2':'')+'" style="min-height:104px;background:'+(any?'var(--accsoft)':'var(--soft)')+';border:1px solid var(--line)"><p class="font-semibold text-sm">'+esc(r)+'</p>';
    h+=ds.length?'<div class="flex flex-wrap gap-2 mt-2">'+ds.map(function(x){return '<span class="ibox" style="width:34px;height:34px;border-radius:11px;color:'+(x.d.on?'var(--acc)':'var(--mute)')+'">'+ico(x.d.type)+'</span>'}).join('')+'</div>':'<p class="mute text-xs">Нет устройств</p>';
    h+='</button>';
  });
  $('plan').innerHTML=h;
}

function renderHome(){
  var h='';
  ROOMS.forEach(function(r,k){
    var ds=devsOf(r),on=ds.filter(function(x){return x.d.on}).length;
    h+='<button data-room="'+k+'" class="glass hov p-4 text-left"><p class="font-semibold">'+esc(r)+'</p><p class="num text-3xl mt-3">'+ds.length+'</p><p class="mute text-xs mt-1">'+(ds.length?'Работают: '+on:'Нет устройств')+'</p></button>';
  });
  $('home-rooms').innerHTML=h;
}

function renderProf(){
  document.body.classList.toggle('is-admin',isAdmin);
  var t=isAdmin?'Администратор':'Пользователь';
  $('prof-role').textContent=t; $('prof-status').textContent=t;
  $('prof-login').style.display=isAdmin?'none':'';
  $('prof-admin').style.display=isAdmin?'':'none';
  $('user-list').innerHTML=USERS.map(function(u){return '<div class="rounded-2xl p-3 flex items-center gap-3" style="background:var(--soft);border:1px solid var(--line)"><span class="ibox" style="width:36px;height:36px;border-radius:50%;color:var(--mute)"><svg class="ico" style="width:18px;height:18px"><use href="#i-user"/></svg></span><span class="flex-1 font-semibold text-sm">'+esc(u.n)+'</span><span class="mute text-xs">'+u.r+'</span></div>'}).join('');
}
function setAdmin(v){isAdmin=v;$('adm-pass').value='';$('adm-err').style.display='none';$('user-note').style.display='none';renderProf()}

function openRoom(k){curRoom=k;renderList();showTab('s-room')}

function tile(a,b){return '<div class="rounded-2xl p-3" style="background:var(--soft);border:1px solid var(--line)"><p class="mute text-xs font-semibold">'+a+'</p><p class="font-semibold mt-0.5">'+b+'</p></div>'}
function ctlHTML(d){
  var t=TYPES[d.type],h='';
  if(d.type==='humid')h+='<div class="grid grid-cols-2 gap-2 mt-5">'+tile('Сейчас в комнате','45%')+tile('Уровень воды','70%')+'</div>';
  if(d.type==='vacuum')h+='<div class="grid grid-cols-2 gap-2 mt-5">'+tile('Заряд','84%')+tile('Последняя уборка','Сегодня, 09:42')+'</div>';
  if(d.type==='lamp')h+='<div class="seg mt-5"><button data-pwr="1" class="'+(d.on?'on':'')+'">Включить</button><button data-pwr="0" class="'+(d.on?'':'on')+'">Выключить</button></div>';
  if(t.s)h+='<div class="mt-6 flex items-end justify-between"><p class="mute text-sm font-semibold">'+t.s.l+'</p><p id="d-val" class="num text-4xl">'+d.val+t.s.u+'</p></div><input id="d-range" type="range" min="'+t.s.min+'" max="'+t.s.max+'" value="'+d.val+'" class="mt-3">';
  if(d.type==='humid'){
    h+='<p class="mute text-sm font-semibold mt-5 mb-2">Режим вентилятора</p><div class="flex gap-2">';
    ['Тихий','Авто','Макс'].forEach(function(m){h+='<button data-mode="'+m+'" class="pill'+(d.mode===m?' on':'')+'">'+m+'</button>'});
    h+='</div>';
  }
  if(d.type==='vacuum'){
    h+='<div class="flex justify-center gap-6 mt-6"><button data-vac="1" aria-label="Начать уборку" class="btn btn-acc rbtn'+(d.on?' off':'')+'"><svg class="ico"><use href="#i-play"/></svg></button><button data-vac="0" aria-label="Закончить и вернуться на станцию" class="btn btn-soft rbtn'+(d.on?'':' off')+'"><svg class="ico"><use href="#i-home"/></svg></button></div>';
    var s=d.sched;
    h+='<p class="mute text-sm font-semibold mt-5 mb-2">Расписание уборки</p><div class="seg"><button data-smode="cal" class="'+(s.mode==='cal'?'on':'')+'">Календарь</button><button data-smode="timer" class="'+(s.mode==='timer'?'on':'')+'">Таймер</button></div>';
    if(s.mode==='cal'){
      h+='<div class="flex items-center justify-between mt-4"><span class="text-sm font-semibold">Время запуска</span><input id="d-time" type="time" value="'+s.time+'" class="field" style="width:auto"></div><div class="flex justify-between mt-4">';
      DN.forEach(function(n,i){h+='<button data-day="'+i+'" class="day'+(s.days.indexOf(i)>-1?' on':'')+'">'+n+'</button>'});
      h+='</div>';
    }else{
      h+='<div class="mt-4 flex items-end justify-between"><span class="mute text-sm font-semibold">Начать через</span><span id="d-tval" class="num text-2xl">'+s.timer+' мин</span></div><input id="d-timer" type="range" min="5" max="120" step="5" value="'+s.timer+'" class="mt-2">';
    }
  }
  return h;
}
function renderCtl(){$('d-ctl').innerHTML=ctlHTML(DEV[cur])}

function paintStatus(){
  var d=DEV[cur],v=d.type==='vacuum',own=v||d.type==='lamp';
  $('d-status').textContent=d.on?(v?'Убирает':'Включено'):(v?'На базе':'Выключено');
  $('d-status').style.color=d.on?'var(--ok)':'var(--mute)';
  $('d-toggle').style.display=own?'none':'';
  $('d-toggle').textContent=d.on?'Выключить':'Включить';
  $('d-toggle').className='btn w-full mt-6 rounded-2xl py-3.5 '+(d.on?'btn-soft':'btn-acc');
}
function openDev(i){
  cur=i; var d=DEV[i];
  $('d-name').textContent=d.name; $('d-room').textContent=d.room;
  $('d-icon').innerHTML=ico(d.type); $('d-icon').style.color='var(--acc)';
  renderCtl(); paintStatus(); $('m-dev').classList.add('open');
}

function showTab(id){
  document.querySelectorAll('.screen').forEach(function(s){s.classList.toggle('active',s.id===id)});
  var t=id==='s-room'?'s-dev':id;
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.toggle('active',b.dataset.tab===t)});
  window.scrollTo(0,0);
}
function fillRooms(sel){$('add-room').innerHTML=ROOMS.map(function(r){return '<option>'+esc(r)+'</option>'}).join('');$('add-room').value=sel}

/* события */
document.querySelectorAll('[data-tab]').forEach(function(b){b.addEventListener('click',function(){showTab(b.dataset.tab)})});
document.querySelectorAll('[data-modal]').forEach(function(b){b.addEventListener('click',function(){$(b.dataset.modal).classList.add('open')})});
document.addEventListener('click',function(e){var b=e.target.closest('[data-room]');if(b)openRoom(+b.dataset.room)});
$('adm-login').addEventListener('click',function(){
  if($('adm-pass').value===PASS)setAdmin(true);else $('adm-err').style.display='block';
});
$('adm-pass').addEventListener('keydown',function(e){if(e.key==='Enter')$('adm-login').click()});
$('adm-logout').addEventListener('click',function(){setAdmin(false)});
$('user-add').addEventListener('click',function(){$('user-note').style.display='block'});
$('room-back').addEventListener('click',function(){showTab('s-dev')});
$('room-add').addEventListener('click',function(){fillRooms(ROOMS[curRoom]);$('m-add').classList.add('open')});
$('room-submit').addEventListener('click',function(){
  var n=$('room-name').value.trim();
  if(n&&ROOMS.indexOf(n)<0)ROOMS.push(n);
  $('room-name').value='';renderList();$('m-room').classList.remove('open');
});
document.querySelectorAll('.modal').forEach(function(m){
  m.addEventListener('click',function(e){if(e.target===m||e.target.closest('[data-close]'))m.classList.remove('open')});
});
$('dev-list').addEventListener('click',function(e){var b=e.target.closest('[data-dev]');if(b)openDev(+b.dataset.dev)});
$('d-toggle').addEventListener('click',function(){DEV[cur].on=DEV[cur].on?0:1;paintStatus();renderList()});
$('d-ctl').addEventListener('input',function(e){
  var d=DEV[cur],id=e.target.id;
  if(id==='d-range'){d.val=+e.target.value;$('d-val').textContent=d.val+TYPES[d.type].s.u}
  if(id==='d-timer'){d.sched.timer=+e.target.value;$('d-tval').textContent=d.sched.timer+' мин'}
  if(id==='d-time'){d.sched.time=e.target.value||d.sched.time}
  renderList();
});
$('d-ctl').addEventListener('click',function(e){
  var d=DEV[cur],b;
  if(b=e.target.closest('[data-mode]'))d.mode=b.dataset.mode;
  else if(b=e.target.closest('[data-pwr]'))d.on=+b.dataset.pwr;
  else if(b=e.target.closest('[data-vac]'))d.on=+b.dataset.vac;
  else if(b=e.target.closest('[data-smode]'))d.sched.mode=b.dataset.smode;
  else if(b=e.target.closest('[data-day]')){var k=+b.dataset.day,i=d.sched.days.indexOf(k);if(i<0)d.sched.days.push(k);else d.sched.days.splice(i,1)}
  else return;
  renderCtl();paintStatus();renderList();
});
document.querySelectorAll('.type-btn').forEach(function(b){
  b.addEventListener('click',function(){addType=b.dataset.type;document.querySelectorAll('.type-btn').forEach(function(x){x.classList.toggle('sel',x===b)})});
});
$('add-submit').addEventListener('click',function(){
  var t=TYPES[addType],d={name:$('add-name').value.trim()||t.t,room:$('add-room').value,type:addType,on:addType==='vacuum'?0:1,val:t.s?t.s.v:0};
  if(addType==='humid')d.mode='Авто';
  if(addType==='vacuum')d.sched={mode:'cal',time:'10:00',days:[0,1,2,3,4],timer:30};
  DEV.push(d); $('add-name').value=''; renderList(); $('m-add').classList.remove('open');
});

/* старт */
var now=new Date(),hr=now.getHours(),ds=now.toLocaleDateString('ru-RU',{weekday:'long',day:'numeric',month:'long'});
$('greet').textContent=hr<5?'Доброй ночи':hr<12?'Доброе утро':hr<18?'Добрый день':'Добрый вечер';
$('date').textContent=ds.charAt(0).toUpperCase()+ds.slice(1);
renderList(); renderProf(); showTab('s-home');
