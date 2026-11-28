const $=id=>document.getElementById(id);
let token='',refresh='',generation=0,pending='',stream;
let refreshingToken;
const notificationDuration = 7000;
let notificationTimer;

function msg(message) {
 clearTimeout(notificationTimer);
 const status = $('status');
 status.textContent = message;
 if (message) {
   notificationTimer = setTimeout(() => {
     status.textContent = '';
   }, notificationDuration);
 }
}
const authHeaders=()=>({'Content-Type':'application/json',Authorization:'Bearer '+token});
async function api(path,body,method=body?'POST':'GET',retry=true){
 const requestToken = token;
 const r = await fetch('/api/' + path, {method, headers: authHeaders(), body: body ? JSON.stringify(body) : undefined});
 if (r.status === 401 && retry && refresh && path !== 'auth/refresh') {
   if (token === requestToken) {
     refreshingToken ??= api('auth/refresh', {refresh_token: refresh}, 'POST', false)
       .then(grant => { token = grant.access_token; })
       .finally(() => { refreshingToken = undefined; });
     await refreshingToken;
   }
   return api(path, body, method, false);
 }
 const data = await r.json().catch(() => null);
 if (!r.ok) throw Error(typeof data?.detail === 'string' ? data.detail : 'Ошибка сервера (HTTP ' + r.status + ')');
 if (!data) throw Error('Сервер вернул ответ неизвестного формата');
 return data;
}
function form(id,fn){$(id).onsubmit=async e=>{e.preventDefault();try{await fn(Object.fromEntries(new FormData(e.target)))}catch(err){msg(err.message)}}}
function el(tag,text){const n=document.createElement(tag);n.textContent=text;return n}
function btn(label,fn){const b=el('button',label);b.onclick=async()=>{b.disabled=true;try{await fn()}catch(e){msg(e.message)}finally{b.disabled=false}};return b}
function commandStatus(response) {
 return [response.status, response.reason].filter(Boolean).join(' · ');
}
function result(response) {
 msg(commandStatus(response));
}
let ROOMS=[], DEV=[], roomRecords=[], currentRoom='', currentDevice='';
function ico(name){return '<svg class="ico"><use href="#i-'+name+'"/></svg>'}
function esc(value){return String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;')}
function devsOf(room){return DEV.filter(d=>d.room===room).map(d=>({d}))}
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

function showTab(id){
  document.querySelectorAll('.screen').forEach(function(s){s.classList.toggle('active',s.id===id)});
  var t=id==='s-room'?'s-dev':id;
  document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.toggle('active',b.dataset.tab===t)});
  window.scrollTo(0,0);
}

document.addEventListener('click', e=>{
 const b=e.target.closest('button');
 if(!b)return;
 if(b.dataset.tab)showTab(b.dataset.tab);
 if(b.id==='room-back')showTab('s-dev');
 if(b.dataset.room!==undefined){currentRoom=ROOMS[Number(b.dataset.room)];renderRoom();showTab('s-room')}
 if(b.dataset.modal) {
   $(b.dataset.modal).classList.add('open');
   if (b.dataset.modal === 'm-add') loadAvailable();
 }
 if(b.hasAttribute('data-close'))b.closest('.modal').classList.remove('open');
});
async function loadAvailable() {
 const note = $('catalog-note');
 const submit = $('add-submit');
 submit.disabled = true;
 note.textContent = 'Ищем устройства…';
 try {
   const {devices} = await api('catalog/available');
   const room = roomRecords.find(item => item.name === currentRoom);
   $('add-device').replaceChildren(...devices.map(d => new Option(d.name, d.id)));
   $('add-room').replaceChildren(...roomRecords.map(r => new Option(r.name, r.id)));
   if (room) $('add-room').value = room.id;
   submit.disabled = !devices.length || !roomRecords.length;
   note.textContent = !devices.length ? 'Все устройства уже подключены.' :
     !roomRecords.length ? 'Сначала добавьте комнату.' : '';
 } catch (error) {
   note.textContent = error.message;
 }
}
form('create-room', async data => {
 await api('rooms', data);
 $('create-room').reset();
 $('m-room').classList.remove('open');
 await update();
 msg('Комната добавлена');
});
form('connect-device', async data => {
 const submit = $('add-submit');
 submit.disabled = true;
 try {
   await api('devices/' + encodeURIComponent(data.device_id) + '/connection', {room_id: data.room_id}, 'PUT');
   $('m-add').classList.remove('open');
   await update();
   msg('Устройство подключено');
 } finally {
   submit.disabled = false;
 }
});
$('room-remove').onclick = async () => {
 const room = roomRecords.find(item => item.name === currentRoom);
 if (!room || !window.confirm('Удалить комнату «' + room.name + '»?')) return;
 try {
   await api('rooms/' + encodeURIComponent(room.id), null, 'DELETE');
   currentRoom = '';
   showTab('s-dev');
   await update();
   msg('Комната удалена');
 } catch (error) {
   msg(error.message);
 }
};
function renderRoom(){
 const items=devsOf(currentRoom);
 $('room-title').textContent=currentRoom;
 $('room-sub').textContent='Устройств: '+items.length;
 $('room-remove').hidden = !currentRoom || items.length > 0;
 $('dev-list').replaceChildren(...items.map(({d})=>{
 const b=btn('',()=>{currentDevice=d.id;$('d-feedback').textContent='';renderSettings(d);paintDevice();$('m-dev').classList.add('open')});
 b.className='glass hov w-full p-4 flex items-center gap-4 text-left';
 b.innerHTML='<span class="ibox">'+ico(d.type)+'</span><strong class="flex-1">'+esc(d.name)+'</strong><span class="chip">'+esc(d.status)+'</span>';
 return b;
 }));
}
let deviceActionInFlight = false;
async function runDeviceAction(device, operation, params = {}) {
 if (deviceActionInFlight) return;
 deviceActionInFlight = true;
 const feedback = $('d-feedback');
 feedback.textContent = 'Ожидаем ответ устройства…';
 paintDevice();
 try {
   const response = await api('commands', {device_id: device.id, operation, params});
   if (currentDevice === device.id) feedback.textContent = [response.status, response.reason].filter(Boolean).join(' · ');
   await update();
 } catch (error) {
   if (currentDevice === device.id) feedback.textContent = error.message;
 } finally {
   deviceActionInFlight = false;
   paintDevice();
 }
}
const editableSettings = {
 boiler: [{key: 'coolant_temperature', title: 'Целевая температура воды, °C', operation: 'set_coolant_temperature', min: 30, max: 80}],
 conditioner: [
   {key: 'target_temp', title: 'Целевая температура воздуха, °C', operation: 'set_target_temperature', min: 16, max: 30},
   {key: 'mode', title: 'Режим кондиционера', operation: 'set_mode',
    choices: {auto: 'Авто', cool: 'Охлаждение', heat: 'Обогрев', fan: 'Вентиляция'}},
 ],
 light: [{key: 'brightness', title: 'Яркость, %', operation: 'set_brightness', min: 1, max: 100}],
};
function renderSettings(device) {
 const settings = editableSettings[device.kind] || [];
 $('d-settings').replaceChildren(...settings.map(setting => {
   const row = document.createElement('form');
   row.className = 'catalog-form';
   const label = el('label', setting.title);
   const input = document.createElement(setting.choices ? 'select' : 'input');
   input.className = 'field';
   if (setting.choices) {
     input.replaceChildren(...Object.entries(setting.choices).map(([value, name]) => new Option(name, value)));
   } else {
     Object.assign(input, {type: 'number', required: true, min: setting.min, max: setting.max, step: '1'});
   }
   input.value = device.state[setting.key] ?? '';
   const save = el('button', 'Сохранить');
   save.className = 'btn btn-soft rounded-xl py-2';
   row.onsubmit = event => {
     event.preventDefault();
     const value = setting.choices ? input.value : Number(input.value);
     runDeviceAction(device, setting.operation, {value});
   };
   label.append(input);
   row.append(label, save);
   return row;
 }));
}
$('d-disconnect').onclick = async () => {
 const device = DEV.find(item => item.id === currentDevice);
 if (!device || !window.confirm('Отключить «' + device.name + '»? Связанная автоматизация будет удалена.')) return;
 try {
   await api('devices/' + encodeURIComponent(device.id) + '/connection', {room_id: null}, 'PUT');
   $('m-dev').classList.remove('open');
   await update();
   msg('Устройство отключено');
 } catch (error) {
   $('d-feedback').textContent = error.message;
 }
};
function paintDevice(){
 const d=DEV.find(x=>x.id===currentDevice);
 if (!d) {
   if (currentDevice) $('m-dev').classList.remove('open');
   return;
 }
 $('d-name').textContent=d.name;$('d-room').textContent=d.room;$('d-icon').innerHTML=ico(d.type);
 $('d-status').textContent=d.status;
 const labels={power:'Питание',value:d.kind === 'sensor_temperature' ? 'Температура, °C' : 'Показание',water:'Клапан',pressure:'Давление, бар',temperature:d.kind === 'conditioner' ? 'Текущая температура воздуха, °C' : 'Текущая температура, °C',target_temp:'Целевая температура воздуха, °C',coolant_temperature:'Целевая температура воды, °C',brightness:'Яркость, %',mode:'Режим',battery:'Заряд, %',mains:'Питание от сети',jammed:'Привод заклинило'};
 $('d-ctl').replaceChildren(...Object.entries(d.state).map(([k,v])=>el('p',(labels[k]||k)+' · '+(v===true?'да':v===false?'нет':v??'—'))),el('small',d.updated_at?new Date(d.updated_at).toLocaleString()+(d.stale?' · устарело':''):'Ожидание показаний'));
 if(d.lock)$('d-ctl').append(btn('Вернуть автоматику',async()=>{await api('devices/'+d.id+'/release-lock',{});await update()}));
 const valve=d.kind==='leak_protection', toggle=$('d-toggle');
 const faultBlocksToggle = Boolean(d.error) && !(valve && d.on && d.error === 'E22');
 const leak = DEV.find(device => device.kind === 'sensor_leak');
 const unsafeToOpen = valve && !d.on && (!leak || leak.stale || leak.state.value !== false);
 const warning = $('d-warning');
 warning.textContent = d.error
   ? 'Ошибка ' + d.error + '. Устраните причину в Godot и выберите «Нет ошибки».'
   : unsafeToOpen
     ? (leak?.stale || !leak ? 'Открытие клапана заблокировано. Нет свежих показаний датчика протечки.' : 'Открытие клапана заблокировано. Датчик фиксирует протечку.')
     : '';
 warning.hidden = !warning.textContent;
 toggle.disabled = deviceActionInFlight || faultBlocksToggle || unsafeToOpen;
 $('d-disconnect').disabled = deviceActionInFlight;
 for (const control of $('d-settings').querySelectorAll('input, select, button')) {
   control.disabled = deviceActionInFlight || Boolean(d.error);
 }
 toggle.hidden=!valve && !['conditioner','boiler','light'].includes(d.kind);
 toggle.className='btn btn-acc w-full mt-6 rounded-2xl py-3.5';
 toggle.textContent=valve?(d.on?'Закрыть':'Открыть'):(d.on?'Выключить':'Включить');
 const operation = valve ? (d.on ? 'close_water' : 'open_water') : (d.on ? 'turn_off' : 'turn_on');
 toggle.onclick = () => runDeviceAction(d, operation);
}
async function update(){
 const n=++generation;const [h,j]=await Promise.all([api('home'),api('journal?limit=18')]);if(n!==generation)return;
 roomRecords=h.rooms;
 ROOMS=h.rooms.map(r=>r.name);
 const icons={conditioner:'ac',light:'lamp',humidifier:'humid',socket:'plug',boiler:'boiler',vacuum:'vacuum'};
 DEV=h.rooms.flatMap(r=>r.devices.map(d=>({...d,room:r.name,kind:d.type,type:icons[d.type]||'sensor',on:d.state.power===true||d.state.water==='open',status:d.error|| (d.state.power===true?'Вкл':d.state.power===false?'Выкл':String(d.state.value??d.state.water??'—'))})));
 for(const [key,unit] of [['temperature','°'],['humidity','%']]){
 const sensor=DEV.find(d=>d.kind==='sensor_'+key && d.state.value!=null && !d.stale);
 $(key).textContent=sensor?sensor.state.value+unit:'—';$(key).title=sensor?.room||'Нет свежих данных';
 }
 $('cnt-on').textContent=DEV.filter(d=>d.on).length;$('cnt-all').textContent=DEV.length;
 if (currentRoom && !ROOMS.includes(currentRoom)) {
   currentRoom = '';
   if ($('s-room').classList.contains('active')) showTab('s-dev');
 }
 const ac = DEV.find(device => device.id === 'conditioner_living');
 const room = ac && roomRecords.find(item => item.name === ac.room);
 $('target').hidden = !room;
 $('schedule').hidden = !room;
 if (room) $('target-room').textContent = 'Целевая температура · ' + room.name + ', °C';
 renderMap();renderHome();renderRoom();paintDevice();
 $('rules').replaceChildren(...h.schedules.map(s=>btn(s.at+' UTC · '+s.operation+' · убрать',async()=>{await api('schedules/'+s.id,null,'DELETE');await update()})),...h.rooms.flatMap(r=>r.targets.map(t=>btn(r.name+' · '+t.value+' °C · убрать',async()=>{await api('targets/'+r.id+'/'+t.parameter,null,'DELETE');await update()}))));

 const events = h.history.slice(-8).map(e => ({
   at: e.time,
   parts: [e.device_id, e.kind === 'error_cleared' ? 'Ошибка снята · ' + e.code : e.code || e.kind],
   event: e,
 }));
 events.push(...j.records.map(e => ({at: e.at, parts: [e.subject, e.action, e.status, e.reason]})));
 $('audit').replaceChildren(...events.map(({at, parts, event}) => {
   const row = el('div', parts.filter(Boolean).join(' · '));
   row.className = 'glass p-4';
   row.append(el('small', new Date(at).toLocaleString()));
   if (event && event.kind === 'error' && /^E\d{2}$/i.test(event.code)) {
     row.append(btn('Объяснить ' + event.code, async () => {
       const form = $('chat');
       if (form.querySelector('button').disabled) return;
       form.elements.message.value = 'Что означает ' + event.code + ' у ' + event.device_id + '?';
       showTab('s-chat');
       form.requestSubmit();
     }));
   }
   return row;
 }));
 if (!events.length) $('audit').textContent = 'Событий пока нет';
}
var now=new Date(),hr=now.getHours(),ds=now.toLocaleDateString('ru-RU',{weekday:'long',day:'numeric',month:'long'});
$('greet').textContent=hr<5?'Доброй ночи':hr<12?'Доброе утро':hr<18?'Добрый день':'Добрый вечер';
$('date').textContent=ds.charAt(0).toUpperCase()+ds.slice(1);
showTab('s-home');
async function live(signal) {
 while (!signal.aborted) {
  let reader;
  try {
   await update();
   const response = await fetch('/api/live', {headers: authHeaders(), signal});
   if (!response.ok) throw Error(response.statusText);
   $('link').textContent = 'Симуляция · поток подключён';
   reader = response.body.getReader();
   await update();
   while (!signal.aborted) {
    const {done} = await reader.read();
    if (done) break;
    await update();
   }
  } catch (error) {
   if (signal.aborted) return;
  } finally {
   if (reader) await reader.cancel().catch(() => {});
  }
  if (signal.aborted) return;
  $('link').textContent = 'Восстанавливаем связь…';
  await new Promise(resolve => setTimeout(resolve, 1200));
 }
}
form('login',async f=>{const r=await api('auth/login',f);$('prof-role').textContent=$('prof-status').textContent=r.role==='admin'?'Администратор':'Жилец';document.body.classList.toggle('is-admin',r.role==='admin');token=r.access_token;refresh=r.refresh_token;$('signin').hidden=true;$('house').hidden=false;$('invite').hidden=r.role!=='admin';msg('');stream=new AbortController();live(stream.signal);$('provider').textContent=(await api('mode')).assistant});
form('accept',async f=>{await api('auth/invite/accept',f);msg('Можно войти с новым паролем')});
form('target',async f=>{await api('targets',{room_id:roomRecords.find(room=>room.devices.some(device=>device.id==='conditioner_living'))?.id,parameter:'temperature',value:Number(f.value),device_id:'conditioner_living'});await update()});
form('schedule',async f=>{await api('schedules/command',{...f,device_id:'conditioner_living'});await update()});
form('chat', async fields => {
 const submit = $('chat').querySelector('button[type="submit"], button');
 submit.disabled = true;
 $('reply').hidden = false;
 $('question').textContent = fields.message;
 $('confirm').hidden = true;
 pending = '';
 $('answer').textContent = 'Ищу ответ…';
 $('sources').replaceChildren();
 try {
   const reply = await api('chat', fields);
   $('answer').textContent = reply.reply;
   for (const source of reply.sources) {
     const chunk = source.split('#')[1];
     const detail = reply.source_details?.find(item => item.chunk_id === chunk);
     const label = detail?.section ? detail.source + ' · ' + detail.section : source;
     $('sources').append(btn('Читать ' + label, async () => {
       const document = await api('sources/' + encodeURIComponent(chunk));
       $('sources').append(el('p', document.text));
     }));
   }
   if (reply.result) $('answer').textContent = reply.reply + ' · ' + commandStatus(reply.result);
   pending = reply.confirmation;
   $('confirm').hidden = !pending;
 } catch (error) {
   $('answer').textContent = error.message;
 } finally {
   submit.disabled = false;
 }
});
$('confirm').onclick = async () => {
 if (!pending) return;
 const confirmation = pending;
 const explanation = $('answer').textContent;
 pending = '';
 $('confirm').hidden = true;
 const submit = $('chat').querySelector('button');
 submit.disabled = true;
 $('answer').textContent = 'Ожидаю подтверждение от устройства…';
 try {
   const response = await api('chat/confirm/' + encodeURIComponent(confirmation), {});
   $('answer').textContent = explanation + '\n' + commandStatus(response);
 } catch (error) {
   $('answer').textContent = error.message;
 } finally {
   submit.disabled = false;
 }
};
form('manual-upload', async ({manual}) => {
 const feedback = $('manual-feedback');
 const submit = $('manual-upload').querySelector('button');
 submit.disabled = true;
 feedback.textContent = 'Собираю индекс…';
 try {
   const text = new TextDecoder('utf-8', {fatal: true}).decode(await manual.arrayBuffer());
   const response = await api('knowledge/manuals', {filename: manual.name, text});
   feedback.textContent = 'Руководство добавлено: ' + response.source;
   $('manual-upload').reset();
 } catch (error) {
   feedback.textContent = error.message;
 } finally {
   submit.disabled = false;
 }
});
form('invite',async f=>{await api('auth/admin-mode',{password:f.password});$('code').textContent=(await api('admin/invites',{username:f.username})).code});
$('sessions').onclick=async()=>{try{const {sessions}=await api('auth/sessions');$('devices').replaceChildren(...sessions.map(s=>s.current?el('p',s.device+' · текущая'):btn('Отозвать '+s.device,async()=>{await api('auth/sessions/'+s.id,null,'DELETE');$('sessions').click()})))}catch(e){msg(e.message)}};
$('logout').onclick=async()=>{try{await api('auth/logout',{});stream.abort();location.reload()}catch(e){msg(e.message)}};

form('password',async f=>{await api('auth/password',f);$('password').reset();msg('Пароль изменён')});
