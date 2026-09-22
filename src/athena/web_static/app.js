let csrf="", historyTimer=null, statusTimer=null, musicTimer=null, volumeTimer=null, speakerTimer=null, audioContext=null;
const $=id=>document.getElementById(id);
async function api(path,options={}){options.headers={"Content-Type":"application/json",...(options.headers||{})};if(options.method&&options.method!=="GET")options.headers["X-ATHENA-CSRF"]=csrf;const r=await fetch(path,options);if(!r.ok)throw new Error(await r.text()||`Request failed (${r.status})`);return r.json()}
function serviceView(data){const active=data.service==="active";$('statusDot').className=`dot ${active?'active':data.service==='failed'?'failed':''}`;$('serviceLabel').textContent=data.serviceLabel;$('serviceHeading').textContent=active?'Voice is online':'Voice is offline'}
async function boot(){try{const data=await api('/api/bootstrap');csrf=data.csrf;serviceView(data);$('systemPrompt').value=data.prompts.system;$('memoryPrompt').value=data.prompts.memory;$('login').hidden=true;$('app').hidden=false;startRefresh()}catch(e){$('login').hidden=false;$('app').hidden=true}}
function startRefresh(){clearInterval(statusTimer);statusTimer=setInterval(async()=>{try{serviceView(await api('/api/status'))}catch{}},3000);loadHistory();clearInterval(historyTimer);historyTimer=setInterval(()=>{if($('history').classList.contains('active'))loadHistory()},5000);loadMusic();clearInterval(musicTimer);musicTimer=setInterval(loadMusic,1000);loadVolume()}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})});csrf=d.csrf;$('password').value='';await boot()}catch(err){$('loginError').textContent=err.message}});
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.tab,.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.page).classList.add('active');if(b.dataset.page==='history')loadHistory();if(b.dataset.page==='settings')loadSettings()}));
document.querySelectorAll('[data-service]').forEach(b=>b.addEventListener('click',async()=>{document.querySelectorAll('[data-service]').forEach(x=>x.disabled=true);$('controlMessage').textContent=`Sending ${b.dataset.service} command…`;try{const d=await api('/api/service',{method:'POST',body:JSON.stringify({action:b.dataset.service})});serviceView(d);$('controlMessage').textContent=d.serviceLabel}catch(e){$('controlMessage').textContent=e.message}finally{document.querySelectorAll('[data-service]').forEach(x=>x.disabled=false)}}));
function musicView(d){const playing=!!d.playing,paused=!!d.paused;$('musicState').textContent=paused?'PAUSED':playing?'PLAYING':'IDLE';$('musicState').className=`music-state ${paused?'paused':playing?'playing':''}`;$('musicTitle').textContent=d.title||'No track loaded';$('musicToggle').textContent=paused?'Play':'Pause';$('musicToggle').disabled=!playing;if(document.activeElement!==$('musicVolume')){$('musicVolume').value=d.volume;$('musicVolumeLabel').textContent=`${d.volume}%`}if(d.error)$('musicMessage').textContent=d.error}
async function loadMusic(){try{musicView(await api('/api/music'))}catch(e){$('musicState').textContent='OFFLINE';$('musicState').className='music-state';$('musicMessage').textContent=e.message}}
async function musicAction(action,value){document.querySelectorAll('.music-buttons button').forEach(x=>x.disabled=true);try{const d=await api('/api/music',{method:'POST',body:JSON.stringify({action,...(value===undefined?{}:{value})})});musicView(d);$('musicMessage').textContent=action==='volume'?`Music volume set to ${d.volume}%.`:'Music control applied.'}catch(e){$('musicMessage').textContent=e.message}finally{document.querySelectorAll('.music-buttons button').forEach(x=>x.disabled=false);loadMusic()}}
$('musicToggle').addEventListener('click',()=>musicAction('toggle'));
$('musicNext').addEventListener('click',()=>musicAction('next'));
$('musicStop').addEventListener('click',()=>musicAction('stop'));
$('musicVolume').addEventListener('input',e=>{const value=Number(e.target.value);$('musicVolumeLabel').textContent=`${value}%`;clearTimeout(volumeTimer);volumeTimer=setTimeout(()=>musicAction('volume',value),120)});
function speakerView(d){if(document.activeElement!==$('speakerVolume')){$('speakerVolume').value=d.volume;$('speakerVolumeLabel').textContent=`${d.volume}%`}}
async function loadVolume(){try{speakerView(await api('/api/volume'))}catch(e){$('volumeMessage').textContent=e.message}}
async function volumeAction(value){try{speakerView(await api('/api/volume',{method:'POST',body:JSON.stringify({value})}));$('volumeMessage').textContent=`Speaker volume set to ${value}%.`}catch(e){$('volumeMessage').textContent=e.message}}
$('speakerVolume').addEventListener('input',e=>{const value=Number(e.target.value);$('speakerVolumeLabel').textContent=`${value}%`;clearTimeout(speakerTimer);speakerTimer=setTimeout(()=>volumeAction(value),120)});
function message(text,who,extra=''){const node=document.createElement('div');node.className=`message ${who} ${extra}`;node.textContent=text;$('messages').append(node);$('messages').scrollTop=$('messages').scrollHeight;return node}
function armDeviceAudio(){if($('replyOutput').value!=='device')return;if(!audioContext)audioContext=new (window.AudioContext||window.webkitAudioContext)();audioContext.resume()}
async function speakReply(text){const target=$('replyOutput').value;if(target==='off')return;try{if(target==='athena'){await api('/api/speech/athena',{method:'POST',body:JSON.stringify({text})});return}const r=await fetch('/api/speech/device',{method:'POST',headers:{'Content-Type':'application/json','X-ATHENA-CSRF':csrf},body:JSON.stringify({text})});if(!r.ok)throw new Error(await r.text());const data=await r.arrayBuffer();const decoded=await audioContext.decodeAudioData(data);const source=audioContext.createBufferSource();source.buffer=decoded;source.connect(audioContext.destination);source.start()}catch(e){message(`Audio: ${e.message}`,'athena')}}
async function poll(id,node){for(;;){await new Promise(r=>setTimeout(r,500));try{const d=await api(`/api/chat/${id}`);if(d.status==='complete'){node.classList.remove('pending');node.textContent=d.answer;$('taskCount').textContent='READY';loadHistory();speakReply(d.answer);return}}catch(e){node.classList.remove('pending');node.textContent=e.message;$('taskCount').textContent='ERROR';return}}}
$('chatForm').addEventListener('submit',async e=>{e.preventDefault();const text=$('chatInput').value.trim();if(!text)return;armDeviceAudio();$('chatInput').value='';message(text,'user');const waiting=message('Working in the background…','athena','pending');$('taskCount').textContent='WORKING';try{const d=await api('/api/chat',{method:'POST',body:JSON.stringify({text})});poll(d.id,waiting)}catch(err){waiting.classList.remove('pending');waiting.textContent=err.message;$('taskCount').textContent='ERROR'}});
$('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('chatForm').requestSubmit()}});
async function loadHistory(){try{const d=await api('/api/conversations');const list=$('conversationList');list.replaceChildren();if(!d.conversations.length){const p=document.createElement('p');p.className='muted';p.textContent='No saved conversations yet.';list.append(p);return}for(const row of d.conversations){const box=document.createElement('article');box.className='conversation';const t=document.createElement('time');t.textContent=new Date(row.at).toLocaleString();const q=document.createElement('p');q.className='q';q.textContent=`You: ${row.user}`;const a=document.createElement('p');a.className='a';a.textContent=`ATHENA: ${row.assistant}`;box.append(t,q,a);list.append(box)}}catch(e){$('conversationList').textContent=e.message}}
$('refreshHistory').addEventListener('click',loadHistory);
$('savePrompts').addEventListener('click',async()=>{const b=$('savePrompts');b.disabled=true;$('saveMessage').textContent='Saving…';try{const d=await api('/api/prompts',{method:'POST',body:JSON.stringify({system:$('systemPrompt').value,memory:$('memoryPrompt').value})});$('saveMessage').textContent=d.message}catch(e){$('saveMessage').textContent=e.message}finally{b.disabled=false}});
/* ---- Settings: the same store the voice process reads at every turn ---- */
function settingMessage(text){$('settingsMessage').textContent=text}
function settingControl(spec){
  if(spec.choices&&spec.choices.length){const select=document.createElement('select');for(const option of spec.choices){const opt=document.createElement('option');opt.value=String(option);opt.textContent=String(option);if(String(option)===String(spec.value))opt.selected=true;select.append(opt)}return select}
  if(spec.value_type==='bool'){const box=document.createElement('input');box.type='checkbox';box.checked=!!spec.value;return box}
  const input=document.createElement('input');
  input.type=(spec.value_type==='int'||spec.value_type==='float')?'number':'text';
  if(spec.value_type==='int')input.step='1';if(spec.value_type==='float')input.step='0.1';
  if(spec.minimum!==null)input.min=spec.minimum;if(spec.maximum!==null)input.max=spec.maximum;
  input.value=spec.value;return input
}
function settingRow(name,spec){
  const row=document.createElement('div');row.className='setting-row';
  const head=document.createElement('div');head.className='setting-head';
  const label=document.createElement('span');label.className='setting-name';label.textContent=name;
  const badge=document.createElement('span');badge.className='pill';badge.textContent=spec.applies_live?'LIVE':'RESTART';
  head.append(label,badge);
  const note=document.createElement('p');note.className='muted setting-desc';note.textContent=spec.description;
  const controls=document.createElement('div');controls.className='control-row setting-controls';
  const input=settingControl(spec);input.id=`setting-${name}`;input.setAttribute('aria-label',name);
  const save=document.createElement('button');save.textContent='Save';
  save.addEventListener('click',async()=>{save.disabled=true;settingMessage('');try{const value=input.type==='checkbox'?input.checked:input.value;const d=await api('/api/settings',{method:'POST',body:JSON.stringify({name,value})});settingMessage(`${d.name} set to ${d.value}.${d.applies_live?'':' Restart ATHENA to apply.'}`)}catch(e){settingMessage(e.message)}finally{save.disabled=false}});
  const reset=document.createElement('button');reset.className='ghost';reset.textContent='Reset';
  reset.addEventListener('click',async()=>{reset.disabled=true;settingMessage('');try{await api('/api/settings',{method:'POST',body:JSON.stringify({name,action:'reset'})});settingMessage(`${name} reset to its default.`);loadSettings()}catch(e){settingMessage(e.message)}finally{reset.disabled=false}});
  controls.append(input,save,reset);
  row.append(head,note,controls);return row
}
async function loadSettings(){try{const d=await api('/api/settings');const list=$('settingsList');list.replaceChildren();for(const [name,spec] of Object.entries(d.settings))list.append(settingRow(name,spec));settingMessage('')}catch(e){$('settingsList').textContent=e.message}}
$('refreshSettings').addEventListener('click',loadSettings);
$('logout').addEventListener('click',async()=>{try{await api('/api/logout',{method:'POST',body:'{}'})}catch{}location.reload()});
/* ---- Use this device as ATHENA's microphone and speaker (local network) ----
   Audio goes browser -> dashboard -> voice service over a local Unix socket, so
   no extra port is opened and the dashboard's own session protects it. */
const CAPTURE_WORKLET=`
class Capture extends AudioWorkletProcessor {
  constructor(options){super();this.frameSamples=options.processorOptions.frameSamples;this.ratio=sampleRate/options.processorOptions.targetRate;this.buffer=new Float32Array(0);this.position=0;this.pending=[]}
  process(inputs){
    const channel=inputs[0]&&inputs[0][0];if(!channel)return true;
    const merged=new Float32Array(this.buffer.length+channel.length);merged.set(this.buffer);merged.set(channel,this.buffer.length);
    let position=this.position;const pending=this.pending;
    while(position+1<merged.length){
      const index=Math.floor(position),fraction=position-index;
      pending.push(merged[index]*(1-fraction)+merged[index+1]*fraction);
      position+=this.ratio;
      if(pending.length>=this.frameSamples){
        const frame=new Int16Array(this.frameSamples);
        for(let i=0;i<this.frameSamples;i++){const s=Math.max(-1,Math.min(1,pending[i]));frame[i]=s<0?s*0x8000:s*0x7fff}
        this.port.postMessage(frame.buffer,[frame.buffer]);pending.length=0;
      }
    }
    const consumed=Math.floor(position);this.buffer=merged.slice(consumed);this.position=position-consumed;return true;
  }
}
registerProcessor('athena-capture',Capture);`;
let micSocket=null,micStream=null,micCapture=null,micContext=null,micPlaybackTime=0;
const micSources=new Set();
function micStatus(text,live){$('micStatus').textContent=text;$('micStatus').className=live?'muted mic-live':'muted'}
function playDeviceAudio(buffer){if(!micContext)return;const pcm=new Int16Array(buffer),floats=new Float32Array(pcm.length);for(let i=0;i<pcm.length;i++)floats[i]=pcm[i]/32768;const audioBuffer=micContext.createBuffer(1,floats.length,24000);audioBuffer.copyToChannel(floats,0);const source=micContext.createBufferSource();source.buffer=audioBuffer;source.connect(micContext.destination);const now=micContext.currentTime;if(micPlaybackTime<now+0.06)micPlaybackTime=now+0.06;source.start(micPlaybackTime);micPlaybackTime+=audioBuffer.duration;micSources.add(source);source.onended=()=>micSources.delete(source)}
function flushDeviceAudio(){for(const source of micSources){try{source.stop()}catch(error){/* already ended */}}micSources.clear();micPlaybackTime=0}
async function startDeviceAudio(){
  $('micStart').disabled=true;micStatus('Requesting the microphone…');
  try{micStream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:false}})}
  catch(error){micStatus(`Microphone blocked: ${error.message}`);$('micStart').disabled=false;return}
  micContext=micContext||new (window.AudioContext||window.webkitAudioContext)();
  await micContext.resume();
  const workletUrl=URL.createObjectURL(new Blob([CAPTURE_WORKLET],{type:'application/javascript'}));
  await micContext.audioWorklet.addModule(workletUrl);URL.revokeObjectURL(workletUrl);
  micCapture=new AudioWorkletNode(micContext,'athena-capture',{processorOptions:{targetRate:16000,frameSamples:320}});
  const silence=micContext.createGain();silence.gain.value=0;
  micContext.createMediaStreamSource(micStream).connect(micCapture).connect(silence).connect(micContext.destination);
  const scheme=location.protocol==='https:'?'wss:':'ws:';
  micSocket=new WebSocket(`${scheme}//${location.host}/ws/audio`);micSocket.binaryType='arraybuffer';
  micSocket.onmessage=event=>{if(typeof event.data!=='string'){playDeviceAudio(event.data);return}const control=JSON.parse(event.data);if(control.type==='flush')flushDeviceAudio();if(control.type==='error')micStatus(control.message)};
  micSocket.onopen=()=>{micStatus('Connected — ATHENA is using this device.',true);$('micStop').disabled=false;
    micCapture.port.onmessage=event=>{
      // The meter shows the real captured signal: a bar that moves in a silent
      // room means the wrong input is selected.
      const pcm=new Int16Array(event.data);let sum=0;for(let i=0;i<pcm.length;i++)sum+=pcm[i]*pcm[i];
      $('micMeter').style.width=`${Math.min(100,(Math.sqrt(sum/pcm.length)/32768)*320)}%`;
      if(micSocket&&micSocket.readyState===WebSocket.OPEN)micSocket.send(event.data)}};
  micSocket.onclose=()=>stopDeviceAudio('Disconnected.');
  micSocket.onerror=()=>micStatus('Connection failed. Is the voice service running?');
}
function stopDeviceAudio(reason){
  if(micSocket){micSocket.onclose=null;micSocket.close();micSocket=null}
  if(micCapture){micCapture.port.onmessage=null;micCapture.disconnect();micCapture=null}
  if(micStream){micStream.getTracks().forEach(track=>track.stop());micStream=null}
  flushDeviceAudio();$('micMeter').style.width='0';$('micStart').disabled=false;$('micStop').disabled=true;
  micStatus(reason||'Not connected.');
}
$('micStart').addEventListener('click',startDeviceAudio);
$('micStop').addEventListener('click',()=>stopDeviceAudio());
boot();
