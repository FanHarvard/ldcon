/* Live policy edits change future midpoint requests, never an active scan. */
function windowActionsEditor(bundle,card){
 const section=node('section','','window-actions'),status=node('p','','muted'),controls=node('fieldset'),queue=node('div');
 const enabled=node('input'),agent=node('input'),selection=node('select'),conditions=node('textarea'),parameters=node('textarea'),delay=node('input');
 enabled.type=agent.type='checkbox';conditions.rows=5;parameters.rows=10;delay.type='number';delay.min='0';delay.max='82800';delay.value='0';
 let loaded=null,runtime=null;
 function label(text,input){const l=node('label',text);l.append(input);return l;}
 function job(){const j=cardJob(bundle.id,card.id);if(j?.status!=='running')throw Error('定常サーボを実行してください。');return j;}
 // Match the existing runtime-edit/booking IDs; works on LAN HTTP pages.
 function id(){return [...crypto.getRandomValues(new Uint8Array(16))].map(value=>value.toString(16).padStart(2,'0')).join('');}
 function chosen(){const p=JSON.parse(parameters.value);return {...p,controller_card_id:card.id,mode:'center_only'};}
 async function load(){const j=job();loaded=await api(`jobs/${j.id}/window-actions`);runtime=await api(`jobs/${j.id}/runtime-parameters`);controls.disabled=loaded.status!=='running';
  if(controls.disabled){status.textContent='このworkerは監視・中点探索キューに未対応です。';return;}
  enabled.checked=loaded.config.enabled;agent.checked=loaded.config.agent_enabled;conditions.value=JSON.stringify(loaded.config.conditions,null,2);
  selection.replaceChildren();for(const c of loaded.cards){const o=node('option',c.id);o.value=c.id;selection.append(o);}selection.value=loaded.config.card_id||loaded.cards[0]?.id||'';
  parameters.value=JSON.stringify(loaded.selected_parameters||loaded.cards.find(c=>c.id===selection.value)?.parameters||{},null,2);
  queue.replaceChildren();for(const r of loaded.requests){const line=node('p',`${r.card_id} / ${r.source} / ${r.status} / ${r.reason||''} `);line.append(node('small',r.request_id));
   if(r.status==='queued'){line.append(button('選択中のカード・設定へ変更',async()=>{await api(`jobs/${job().id}/window-actions/${r.request_id}`,'PATCH',{expected_revision:r.revision,card_id:selection.value,parameters:chosen()});await load();}));}
   if(['queued','running','claimed'].includes(r.status)){line.append(button('この探索を取消',async()=>{await api(`jobs/${job().id}/window-actions/${r.request_id}/cancel`,'POST',{expected_revision:r.revision});await load();}));}
   queue.append(line,pre(r.result||{}));}
  status.textContent=`監視 ${enabled.checked?'有効':'無効'} / controller ${loaded.capability_revision}。${JSON.stringify(loaded.rate)}`;
 }
 selection.addEventListener('change',()=>{const c=loaded?.cards.find(c=>c.id===selection.value);if(c)parameters.value=JSON.stringify({...c.parameters,mode:'center_only'},null,2);});
 const save=button('監視条件・次回の探索設定を適用',async()=>{if(!loaded||!runtime)throw Error('現在の設定を読み込んでください。');const j=job();
  const config={...loaded.config,enabled:enabled.checked,agent_enabled:agent.checked,card_id:selection.value,conditions:JSON.parse(conditions.value),parameters:chosen()};
  const result=await api(`jobs/${j.id}/runtime-parameters`,'POST',{request_id:id(),job_id:j.id,card_id:card.id,expected_revision:runtime.revision,patch:{window_guard:config}});
  status.textContent=`${result.status}：適用待ちです。「状態を更新」でworkerの適用結果を確認してください。`;
 });
 const request=button('中点探索を予約',async()=>{const seconds=Number(delay.value);if(!Number.isFinite(seconds)||seconds<0||seconds>82800)throw Error('予約遅延は0〜82800秒です。');
  const result=await api(`jobs/${job().id}/window-actions`,'POST',{request_id:id(),card_id:selection.value,parameters:chosen(),capability_revision:loaded.capability_revision,reason:'Dashboard manual midpoint request',not_before_unix_s:Date.now()/1000+seconds,ttl_s:Math.max(3600,seconds+600)});
  await load();status.textContent=`${result.status}：予約しました。定常ロックを維持し、先行操作が終わってから実行します。`;
 });
 controls.append(label('指標による自動実行',enabled),label('エージェントによる選択済み探索の依頼を許可',agent),label('中点探索カード',selection),
  label('複数スカラーの条件（JSON）',conditions),node('p','条件例: {"field":"dither_response","operator":">","threshold":3,"unit":"fm/mV"}。複数条件・any/all・確認回数・間隔は下のwindow_guard一括編集でも変更できます。側峰はmetrics.side_peak_height_max / metrics.side_peak_prominence_max、幅はmetrics.main_fwhm_fraction（unit: 1）。欠測は発火条件にしません。','muted'),
  label('次回の探索設定（JSON、boundary_limitsはraw単位）',parameters),save,label('手動予約までの秒数',delay),request);
 section.append(node('h3','定常中の監視と中点探索'),node('p','PIと同じowner内で直列実行します。条件・カード・設定の変更は次回から反映し、実行中の探索は変更しません。探索失敗とロックのfatalエラーを区別します。'),button('監視・予約状態を更新',load),controls,status,queue);controls.disabled=true;return section;
}
