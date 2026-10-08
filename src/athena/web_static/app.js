let csrf="", historyTimer=null, statusTimer=null, musicTimer=null, volumeTimer=null, speakerTimer=null, speechTimer=null, audioContext=null;
const $=id=>document.getElementById(id);
async function loadAgents(){try{const d=await api('/api/agents');$('agentList').replaceChildren();for(const a of d.agents||[]){const card=document.createElement('div');card.className='card';const title=document.createElement('h3');title.textContent=`${a.id} — ${a.state}`;const text=document.createElement('p');text.textContent=a.objective;const progress=document.createElement('p');progress.textContent=a.report||a.progress;card.append(title,text,progress);if(['queued','running','waiting'].includes(a.state)){const cancel=document.createElement('button');cancel.type='button';cancel.className='secondary';cancel.textContent='Cancel agent and children';cancel.onclick=async()=>{try{await api('/api/agents',{method:'POST',body:JSON.stringify({action:'cancel',id:a.id})});await loadAgents()}catch(e){$('agentMessage').textContent=e.message}};card.append(cancel)}$('agentList').append(card)}}catch(e){$('agentMessage').textContent=e.message}}
$('refreshAgents').addEventListener('click',loadAgents);
document.querySelector('[data-page="agents"]').addEventListener('click',loadAgents);
$('agentForm').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;try{const d=await api('/api/agents',{method:'POST',body:JSON.stringify({action:'spawn',objective:$('agentObjective').value.trim()})});$('agentMessage').textContent=d.message;await loadAgents()}catch(err){$('agentMessage').textContent=err.message}finally{button.disabled=false}});
async function loadPCBrowser(){try{const d=await api('/api/pc/browser');$('pcKeyboardControl').checked=!!d.keyboard_enabled;$('pcBrowserStatus').textContent=d.error||`PC connected. Keyboard ${d.keyboard_enabled?'enabled':'disabled'}. ${d.url||'No browser page open.'}`}catch(e){$('pcBrowserStatus').textContent=e.message}}
$('refreshPCBrowser').addEventListener('click',loadPCBrowser);
document.querySelector('[data-page="settings"]').addEventListener('click',loadPCBrowser);
$('pcKeyboardControl').addEventListener('change',async()=>{const box=$('pcKeyboardControl');box.disabled=true;try{const d=await api('/api/pc/keyboard',{method:'POST',body:JSON.stringify({enabled:box.checked})});box.checked=!!d.keyboard_enabled;$('pcBrowserStatus').textContent=`PC keyboard/mouse control ${d.keyboard_enabled?'enabled':'disabled'}.`}catch(e){box.checked=false;$('pcBrowserStatus').textContent=e.message}finally{box.disabled=false}});
async function api(path,options={}){options.headers={"Content-Type":"application/json",...(options.headers||{})};if(options.method&&options.method!=="GET")options.headers["X-ATHENA-CSRF"]=csrf;const r=await fetch(path,options);if(!r.ok)throw new Error(await r.text()||`Request failed (${r.status})`);return r.json()}
function serviceView(data){const active=data.service==="active";$('statusDot').className=`dot ${active?'active':data.service==='failed'?'failed':''}`;$('serviceLabel').textContent=data.serviceLabel;$('serviceHeading').textContent=active?'Voice is online':'Voice is offline'}
async function boot(){try{const data=await api('/api/bootstrap');csrf=data.csrf;serviceView(data);$('systemPrompt').value=data.prompts.system;$('memoryPrompt').value=data.prompts.memory;$('login').hidden=true;$('app').hidden=false;startRefresh()}catch(e){$('login').hidden=false;$('app').hidden=true}}
function startRefresh(){clearInterval(statusTimer);statusTimer=setInterval(async()=>{try{serviceView(await api('/api/status'));if($('agents').classList.contains('active'))await loadAgents()}catch{}},3000);loadHistory();clearInterval(historyTimer);historyTimer=setInterval(()=>{if($('history').classList.contains('active')&&historyOffset<=40)loadHistory()},5000);loadMusic();clearInterval(musicTimer);musicTimer=setInterval(loadMusic,1000);loadVolume();loadSpeech();clearInterval(speechTimer);speechTimer=setInterval(loadSpeech,500)}
$('loginForm').addEventListener('submit',async e=>{e.preventDefault();$('loginError').textContent='';try{const d=await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})});csrf=d.csrf;$('password').value='';await boot()}catch(err){$('loginError').textContent=err.message}});
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.tab,.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.page).classList.add('active');if(b.dataset.page==='history')loadHistory();if(b.dataset.page==='evidence')loadEvidence();if(b.dataset.page==='settings')loadSettings()}));
document.querySelectorAll('[data-service]').forEach(b=>b.addEventListener('click',async()=>{document.querySelectorAll('[data-service]').forEach(x=>x.disabled=true);$('controlMessage').textContent=`Sending ${b.dataset.service} command…`;let detached=false;try{const d=await api('/api/service',{method:'POST',body:JSON.stringify({action:b.dataset.service})});serviceView(d);if(d.restartedAll){detached=true;$('controlMessage').textContent='Restarting everything…';watchForReconnect();return}$('controlMessage').textContent=d.serviceLabel}catch(e){$('controlMessage').textContent=e.message}finally{if(!detached)document.querySelectorAll('[data-service]').forEach(x=>x.disabled=false)}}));
function watchForReconnect(){let tries=0;const tick=async()=>{tries++;if(tries>30){$('controlMessage').textContent='Still not back. Reload the page.';document.querySelectorAll('[data-service]').forEach(x=>x.disabled=false);return}try{const d=await api('/api/bootstrap');csrf=d.csrf;serviceView(d);$('controlMessage').textContent='Back online.';document.querySelectorAll('[data-service]').forEach(x=>x.disabled=false)}catch{setTimeout(tick,1500)}};setTimeout(tick,1500)}
function musicView(d){const playing=!!d.playing,paused=!!d.paused;$('musicState').textContent=paused?'PAUSED':playing?'PLAYING':'IDLE';$('musicState').className=`music-state ${paused?'paused':playing?'playing':''}`;$('musicTitle').textContent=d.title||'No track loaded';$('musicToggle').textContent=paused?'Play':'Pause';$('musicToggle').disabled=!playing;if(document.activeElement!==$('musicVolume')){$('musicVolume').value=d.volume;$('musicVolumeLabel').textContent=`${d.volume}%`}if(d.error)$('musicMessage').textContent=d.error;playlistView(d)}
function playlistRow(entry){
  const row=document.createElement('div');row.className='playlist-row';
  const info=document.createElement('div');info.className='playlist-info';
  const name=document.createElement('span');name.className='playlist-name';name.textContent=entry.name;
  const note=document.createElement('span');note.className='playlist-tags';note.textContent=entry.moods&&entry.moods.length?entry.moods.join(' · '):entry.query;
  info.append(name,note);
  const play=document.createElement('button');play.type='button';play.textContent='Play';
  play.addEventListener('click',()=>musicAction('play_playlist',undefined,{name:entry.name}));
  const remove=document.createElement('button');remove.type='button';remove.className='ghost';remove.textContent='Delete';
  remove.addEventListener('click',()=>musicAction('remove_playlist',undefined,{name:entry.name}));
  row.append(info,play,remove);return row
}
function playlistView(d){
  const list=$('playlistList'),playlists=Array.isArray(d.playlists)?d.playlists:[];
  $('playlistCount').textContent=`${playlists.length} SAVED`;
  list.replaceChildren();
  if(!playlists.length){const p=document.createElement('p');p.className='muted';p.textContent='No playlists saved yet.';list.append(p);return}
  for(const entry of playlists)list.append(playlistRow(entry))
}
async function loadMusic(){try{musicView(await api('/api/music'))}catch(e){$('musicState').textContent='OFFLINE';$('musicState').className='music-state';$('musicMessage').textContent=e.message}}
async function musicAction(action,value,extra){document.querySelectorAll('.music-buttons button,.playlist-list button').forEach(x=>x.disabled=true);try{const d=await api('/api/music',{method:'POST',body:JSON.stringify({action,...(value===undefined?{}:{value}),...(extra||{})})});musicView(d);$('musicMessage').textContent=d.message||(action==='volume'?`Music volume set to ${d.volume}%.`:'Music control applied.')}catch(e){$('musicMessage').textContent=e.message}finally{document.querySelectorAll('.music-buttons button,.playlist-list button').forEach(x=>x.disabled=false);loadMusic()}}
$('playlistForm').addEventListener('submit',async e=>{e.preventDefault();const button=e.target.querySelector('button'),moods=$('playlistMoods').value.split(',').map(tag=>tag.trim()).filter(Boolean);button.disabled=true;$('playlistMessage').textContent='Saving…';try{const d=await api('/api/music',{method:'POST',body:JSON.stringify({action:'add_playlist',name:$('playlistName').value.trim(),query:$('playlistQuery').value.trim(),moods})});$('playlistMessage').textContent=d.message||'Playlist saved.';e.target.reset();musicView(d)}catch(err){$('playlistMessage').textContent=err.message}finally{button.disabled=false;loadMusic()}});
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
/* Speaking a reply on this computer costs the Pi nothing and needs no cloud
   round trip, so it is the default. The board's own voice is one menu entry
   away for anyone who prefers it. */
function speechVoices(){try{return window.speechSynthesis.getVoices()||[]}catch(e){return[]}}
function preferredVoice(){
  const voices=speechVoices();
  if(!voices.length)return null;
  const english=voices.filter(v=>/^en/i.test(v.lang||''));
  const pool=english.length?english:voices;
  return pool.find(v=>/natural|neural|aria|jenny|sonia|zira|samantha/i.test(v.name||''))||pool[0]
}
function speakOnThisComputer(text){
  if(!('speechSynthesis'in window)){message('Audio: this browser cannot speak by itself. Choose a different Reply audio option.','athena');return}
  try{
    speechSynthesis.cancel();
    const utterance=new SpeechSynthesisUtterance(text);
    const voice=preferredVoice();
    if(voice)utterance.voice=voice;
    speechSynthesis.speak(utterance)
  }catch(e){message(`Audio: ${e.message}`,'athena')}
}
if('speechSynthesis'in window)speechSynthesis.addEventListener?.('voiceschanged',()=>speechVoices());
async function speakReply(text){const target=$('replyOutput').value;if(target==='off')return;if(target==='computer'){speakOnThisComputer(text);return}try{if(target==='athena'){await api('/api/speech/athena',{method:'POST',body:JSON.stringify({text})});return}const r=await fetch('/api/speech/device',{method:'POST',headers:{'Content-Type':'application/json','X-ATHENA-CSRF':csrf},body:JSON.stringify({text})});if(!r.ok)throw new Error(await r.text());const data=await r.arrayBuffer();const decoded=await audioContext.decodeAudioData(data);const source=audioContext.createBufferSource();source.buffer=decoded;source.connect(audioContext.destination);source.start()}catch(e){message(`Audio: ${e.message}`,'athena')}}
async function poll(id,node){for(;;){await new Promise(r=>setTimeout(r,500));try{const d=await api(`/api/chat/${id}`);if(d.status==='complete'){node.classList.remove('pending');node.textContent=d.answer;$('taskCount').textContent='READY';loadHistory();speakReply(d.answer);return}}catch(e){node.classList.remove('pending');node.textContent=e.message;$('taskCount').textContent='ERROR';return}}}
$('chatForm').addEventListener('submit',async e=>{e.preventDefault();const text=$('chatInput').value.trim();if(!text)return;armDeviceAudio();$('chatInput').value='';message(text,'user');const waiting=message('Working in the background…','athena','pending');$('taskCount').textContent='WORKING';try{const d=await api('/api/chat',{method:'POST',body:JSON.stringify({text})});poll(d.id,waiting)}catch(err){waiting.classList.remove('pending');waiting.textContent=err.message;$('taskCount').textContent='ERROR'}});
$('chatInput').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('chatForm').requestSubmit()}});
let historyOffset=0;
async function loadHistory(older=false){try{const offset=older?historyOffset:0;const d=await api('/api/conversations?offset='+offset);const list=$('conversationList');if(!older)list.replaceChildren();if(!d.conversations.length&&!older){const p=document.createElement('p');p.className='muted';p.textContent='No saved conversations yet.';list.append(p)}for(const row of d.conversations){const box=document.createElement('article');box.className='conversation';const t=document.createElement('time');t.textContent=new Date(row.at).toLocaleString();const q=document.createElement('p');q.className='q';q.textContent='You: '+row.user;const a=document.createElement('p');a.className='a';a.textContent='ATHENA: '+row.assistant;box.append(t,q,a);list.append(box)}historyOffset=offset+d.conversations.length;$('olderHistory').disabled=d.conversations.length<40}catch(e){$('contextMessage').textContent=e.message}}
$('refreshHistory').addEventListener('click',()=>loadHistory());
$('olderHistory').addEventListener('click',()=>loadHistory(true));
$('clearContext').addEventListener('click',async()=>{if(!confirm('Start fresh conversation context? Saved history and long-term memories will remain.'))return;const b=$('clearContext');b.disabled=true;try{const d=await api('/api/context/clear',{method:'POST',body:'{}'});$('contextMessage').textContent=d.message;$('messages').replaceChildren();loadHistory()}catch(e){$('contextMessage').textContent=e.message}finally{b.disabled=false}});
async function loadEvidence(){const list=$('evidenceList');try{const d=await api('/api/web-evidence');list.replaceChildren();if(!d.results.length){list.textContent='No web results captured yet. Ask ATHENA to search, then refresh.';return}for(const row of d.results){const box=document.createElement('details');box.className='conversation';const title=document.createElement('summary');title.textContent=new Date(row.at).toLocaleString()+' — '+row.tool+' — '+(row.success?'returned':'failed')+' — '+(row.request.query||row.request.url||'');const pre=document.createElement('pre');pre.style.whiteSpace='pre-wrap';pre.style.overflowWrap='anywhere';pre.textContent=JSON.stringify(row,null,2);box.append(title,pre);list.append(box)}}catch(e){list.textContent=e.message}}
$('refreshEvidence').addEventListener('click',loadEvidence);
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
/* ---- What the speech pipeline actually heard, straight from the voice service ---- */
function speechView(d){
  const state=String(d.state||'waiting'), stale=!!d.stale;
  const ended=!stale&&d.eos_age!==null&&d.eos_age!==undefined&&d.eos_age<1.5;
  const shown=stale?'stale':ended?'ended':state;
  $('speechState').textContent=stale?'NO DATA':shown.toUpperCase();
  $('speechState').className=`pill speech-${shown}`;
  const heard=String(d.heard||''), final=String(d.transcript||'');
  $('speechHeard').textContent=heard?`Hearing: ${heard}`:'Nothing heard yet.';
  $('speechHeard').classList.toggle('live',!!heard&&!stale);
  $('speechFinal').textContent=final?`Last detected: ${final}`:'';
  const retained=Number(d.retained_seconds||0), dropped=Number(d.dropped_seconds||0);
  const frames=Number(d.retained_frames||0), eos=Number(d.eos_count||0);
  $('speechRetention').textContent=`EOS ${eos} · retained ${retained.toFixed(2)}s in ${frames} frames · dropped ${dropped.toFixed(2)}s`;
  $('speechRetention').className=`muted${dropped>0.001?' speech-loss':''}`
}
async function loadSpeech(){
  try{speechView(await api('/api/audio'))}
  catch(e){$('speechState').textContent='NO DATA';$('speechState').className='pill speech-stale';$('speechRetention').textContent=e.message}
}
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
let micSocket=null,micStream=null,micCapture=null,micContext=null,micPlaybackTime=0,deviceRate=24000,deviceChannels=1,micRouteActive=true;
const micSources=new Set();
function micStatus(text,live){$('micStatus').textContent=text;$('micStatus').className=live?'muted mic-live':'muted'}
/* The voice service announces the shape of each stream before it sends it:
   spoken replies come at 24 kHz mono, music at 48 kHz stereo. Reading one with
   the other's shape is exactly what makes a reply sound like a chipmunk and a
   track sound muffled, so both the rate and the channel count are taken from
   the announcement rather than assumed. */
function useDeviceFormat(control){
  if(!control)return;
  const rate=Number(control.rate||control.speaker_rate||0);
  const channels=Number(control.channels||control.speaker_channels||0);
  if(rate>0)deviceRate=rate;
  if(channels>0)deviceChannels=channels
}
function playDeviceAudio(buffer){
  if(!micContext)return;
  const pcm=new Int16Array(buffer),channels=deviceChannels,rate=deviceRate;
  const frames=Math.floor(pcm.length/channels);
  if(frames<1)return;
  const audioBuffer=micContext.createBuffer(channels,frames,rate);
  for(let channel=0;channel<channels;channel++){
    const floats=new Float32Array(frames);
    for(let frame=0;frame<frames;frame++)floats[frame]=pcm[frame*channels+channel]/32768;
    audioBuffer.copyToChannel(floats,channel)
  }
  const source=micContext.createBufferSource();source.athenaSpeech=(rate===24000&&channels===1);source.buffer=audioBuffer;source.connect(micContext.destination);const now=micContext.currentTime;if(micPlaybackTime<now+0.06)micPlaybackTime=now+0.06;source.start(micPlaybackTime);micPlaybackTime+=audioBuffer.duration;micSources.add(source);source.onended=()=>micSources.delete(source)}
function flushDeviceAudio(){for(const source of micSources){try{source.stop()}catch(error){/* already ended */}}micSources.clear();micPlaybackTime=0}
/* ATHENA asking this computer to say something in its own voice. The board does
   no synthesis at all for these, so the reply is heard here immediately and the
   Pi stays free. The board keeps the turn open until it hears back, because the
   microphone on this same machine would otherwise pick up the answer. */
function reportSpeechDone(ok=true){if(micSocket&&micSocket.readyState===WebSocket.OPEN)micSocket.send(JSON.stringify({type:'speak_done',ok}))}
function speakForAthena(text){
  if(!('speechSynthesis'in window)||!text){reportSpeechDone(false);return}
  try{
    speechSynthesis.cancel();
    const utterance=new SpeechSynthesisUtterance(String(text));
    const voice=preferredVoice();if(voice)utterance.voice=voice;
    let reported=false;
    const report=(ok)=>{if(reported)return;reported=true;reportSpeechDone(ok)};
    utterance.onend=()=>report(true);utterance.onerror=()=>report(false);
    // A voice that never fires `end` must not hold the turn open forever.
    setTimeout(()=>report(false),Math.min(120000,Math.max(4000,String(text).length*90)));
    speechSynthesis.speak(utterance)
  }catch(error){reportSpeechDone(false)}
}
function stopSpeakingForAthena(){try{speechSynthesis.cancel()}catch(error){}reportSpeechDone(false)}
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
  micSocket.onmessage=event=>{if(typeof event.data!=='string'){playDeviceAudio(event.data);return}const control=JSON.parse(event.data);if(control.type==='audio_route'){micRouteActive=control.target==='computer';micStatus(micRouteActive?'Connected — microphone and speaker are on this computer.':'Connected in standby — ATHENA is using the Pi.',true)}if(control.type==='ready'||control.type==='audio_format')useDeviceFormat(control);if(control.type==='flush')flushDeviceAudio();if(control.type==='speak')speakForAthena(control.text);if(control.type==='speak_stop')stopSpeakingForAthena();if(control.type==='error')micStatus(control.message)};
  micSocket.onopen=()=>{micStatus('Connected — ATHENA is using this device.',true);$('micStop').disabled=false;
    // Said out loud so the board knows what it may leave to this machine
    // instead of doing itself.
    micSocket.send(JSON.stringify({type:'capabilities',speech:('speechSynthesis'in window)}));
    micCapture.port.onmessage=event=>{
      // The meter shows the real captured signal: a bar that moves in a silent
      // room means the wrong input is selected.
      const pcm=new Int16Array(event.data);let sum=0;for(let i=0;i<pcm.length;i++)sum+=pcm[i]*pcm[i];
      $('micMeter').style.width=`${Math.min(100,(Math.sqrt(sum/pcm.length)/32768)*320)}%`;
      // Keep capture live for explicit stop commands during playback. The
      // Pi uses a separate interruption recognizer, not conversational STT.
      if(micRouteActive&&micSocket&&micSocket.readyState===WebSocket.OPEN)micSocket.send(event.data)}};
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
$('audioToComputer').addEventListener('click',async()=>{try{if(!micSocket||micSocket.readyState!==WebSocket.OPEN){micStatus('Click Start and allow your microphone first.');return}await api('/api/audio-route',{method:'POST',body:JSON.stringify({target:'computer'})})}catch(error){micStatus(error.message)}});
$('audioToPi').addEventListener('click',async()=>{try{await api('/api/audio-route',{method:'POST',body:JSON.stringify({target:'pi'})})}catch(error){micStatus(error.message)}});
boot();
