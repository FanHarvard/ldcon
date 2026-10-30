'use strict';
(() => {
  const area = document.querySelector('#teams-settings');
  if (!area) return;
  const fleet = area.dataset.teamsScope === 'fleet';
  const endpoint = location.pathname.replace(/\/?$/, '/') + 'api/teams-destinations';
  const words = {
    title:['Teams notification destinations','Teams通知先'],
    intro:['Set a shared default and optional per-laser overrides. Saving does not start notification services or send a message.','共通の既定値とレーザー別の上書きを設定します。保存だけでは通知サービスを起動せず、メッセージも送信しません。'],
    default:['Default for all lasers','全レーザー共通の既定値'],
    laser:['Laser','対象レーザー'], mode:['Destination','通知先の選択'],
    inherit:['Use shared default','共通の既定値を使う'],custom:['Use a webhook URL','個別のWebhook URLを使う'],
    disabled:['Do not send notifications','通知しない'],legacy:['Existing environment setting','従来の環境変数設定を使う'],
    url:['New webhook URL','新しいWebhook URL'],
    hint:['Saved URLs are never displayed. Leave blank to keep this saved URL. Switching away from a custom URL removes it from this scope.','保存済みURLは表示しません。空欄ならこの設定の保存済みURLを保持します。個別URL以外へ切り替えると、この設定のURLは削除されます。'],
    configured:['Webhook saved','URL設定済み'],missing:['No webhook saved','URL未設定'],
    inherited:['Using shared default','共通の既定値を使用中'],
    off:['Delivery disabled by destination setting','通知先設定で送信無効'],
    legacyStatus:['Using the notification card’s environment reference','通知カードの環境変数を参照'],
    save:['Save destination','通知先を保存'],reload:['Reload settings','設定を再読み込み'],
    loaded:['Destinations loaded. URLs are hidden.','通知先設定を読み込みました。URLは非表示です。'],
    saved:['Saved. Notification services keep their current state; no message was sent.','保存しました。通知サービスの状態は変更しておらず、メッセージも送信していません。'],
    conflict:['Settings changed elsewhere. Reload before saving again.','別の画面で設定が変更されました。再読み込みしてから保存してください。'],
    invalid:['Check the mode and URL. Use a Microsoft Teams/Workflows HTTPS webhook; a custom destination requires a saved or new URL.','通知先とURLを確認してください。Microsoft Teams/WorkflowsのHTTPS URLを指定します。個別URLには保存済みまたは新しいURLが必要です。'],
    failed:['Could not read or save destinations. Reload to check the current state.','通知先の読取りまたは保存に失敗しました。再読み込みで現在の状態を確認してください。'],
    globalHelp:['Edit the shared default from Other settings on the all-lasers dashboard.','共通の既定値は全レーザー画面の「他設定」から編集できます。'],
    busy:['Saving…','保存中…'], override:['Per-laser override','レーザー別の上書き'],
  };
  const labels = [];
  const t = key => words[key][document.documentElement.lang === 'ja' ? 1 : 0];
  function element(tag, key) { const e = document.createElement(tag); if (key) { labels.push([e,key]); e.textContent=t(key); } return e; }
  function label(key, input) { const e=element('label'), text=element('span',key); e.append(text,input); return e; }
  function option(key,value) { const e=element('option',key); e.value=value; return e; }
  area.append(element('h2','title'),element('p','intro'));
  if(!fleet) area.append(element('p','globalHelp'));
  const form=element('form'), fields=element('fieldset'); fields.disabled=true;
  fields.style.cssText='border:0;padding:0;margin:0;min-width:0';
  const scope=element('select');scope.setAttribute('aria-label',t('laser'));
  const mode=element('select');mode.setAttribute('aria-label',t('mode'));
  const secret=element('input');secret.type='password';secret.autocomplete='new-password';secret.maxLength=8192;secret.spellcheck=false;
  const saved=element('p');saved.className='muted';
  const submit=element('button','save');submit.type='submit';submit.className='primary';
  const reload=element('button','reload');reload.type='button';
  const status=element('p');status.setAttribute('role','status');
  fields.append(label('laser',scope),label('mode',mode),saved,label('url',secret),element('p','hint'),submit);
  form.append(fields,reload,status);area.append(form);
  let documentValue=null, busy=false, statusKey='', savedKey='';
  const showStatus=key=>{statusKey=key;status.textContent=t(key);};
  function updateSecret() {secret.disabled=mode.value!=='custom';if(secret.disabled)secret.value='';}
  function renderScope() {
    const isGlobal=scope.value==='@default';
    const value=isGlobal?documentValue.default:documentValue.lasers.find(row=>row.id===scope.value);
    if(!value)return;
    mode.replaceChildren(...(isGlobal?[option('custom','custom'),option('disabled','disabled'),...(value.mode==='legacy'?[option('legacy','legacy')]:[])]:[option('inherit','inherit'),option('custom','custom'),option('disabled','disabled')]));
    mode.value=value.mode;secret.value='';
    const effective=value.effective_mode||value.mode;
    savedKey=effective==='disabled'?'off':effective==='legacy'?'legacyStatus':(value.effective_configured??value.configured)?'configured':'missing';
    saved.textContent=t(savedKey);updateSecret();
  }
  function render(value) {
    const selected=scope.value; documentValue=value;
    scope.replaceChildren();if(fleet)scope.append(option('default','@default'));
    for(const row of value.lasers){const opt=element('option');opt.value=row.id;opt.textContent=row.name;scope.append(opt);}
    if([...scope.options].some(opt=>opt.value===selected))scope.value=selected;
    fields.disabled=!scope.options.length; renderScope();
  }
  async function load() {
    if(busy)return;busy=true;fields.disabled=true;reload.disabled=true;
    try {const response=await fetch(endpoint);if(!response.ok)throw Error();render(await response.json());showStatus('loaded');}
    catch(_){showStatus('failed');}
    finally{busy=false;reload.disabled=false;}
  }
  scope.onchange=renderScope;mode.onchange=updateSecret;reload.onclick=load;
  form.onsubmit=async event=>{
    event.preventDefault();if(busy||!documentValue)return;
    const payload={expected_revision:documentValue.revision,laser_id:scope.value==='@default'?null:scope.value,mode:mode.value,webhook_url:secret.value.trim()};
    busy=true;fields.disabled=true;reload.disabled=true;showStatus('busy');
    try {const response=await fetch(endpoint,{method:'PUT',headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:JSON.stringify(payload)});
      secret.value='';payload.webhook_url='';
      if(!response.ok){showStatus(response.status===409?'conflict':response.status===422?'invalid':'failed');return;}
      render(await response.json());showStatus('saved');
    }catch(_){secret.value='';payload.webhook_url='';showStatus('failed');}
    finally{busy=false;fields.disabled=!documentValue;reload.disabled=false;}
  };
  new MutationObserver(()=>{
    for(const [e,key] of labels) e.textContent=t(key);
    scope.setAttribute('aria-label',t('laser'));mode.setAttribute('aria-label',t('mode'));
    if(statusKey)status.textContent=t(statusKey);if(savedKey)saved.textContent=t(savedKey);
  }).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  void load();
})();
