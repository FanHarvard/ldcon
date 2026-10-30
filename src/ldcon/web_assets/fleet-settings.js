'use strict';
(() => {
  const root=location.pathname.replace(/\/?$/, '/'), form=document.querySelector('#fleet-defaults-form');
  if(!form)return;
  let revision=null, loaded=false, busy=false;
  const $=s=>document.querySelector(s), fields=['#default-retention-enabled','#default-retention-days','#default-backup-directory'];
  const message=text=>{$('#fleet-defaults-status').textContent=text;};
  async function load(){
    if(busy)return;busy=true;form.querySelector('fieldset').disabled=true;
    try{const response=await fetch(root+'api/operations-defaults');const value=await response.json();if(!response.ok)throw Error(value.detail||'設定を取得できません。');
      revision=value.revision;loaded=true;$('#default-retention-enabled').checked=value.retention.enabled;$('#default-retention-days').value=value.retention.retention_days;$('#default-backup-directory').value=value.retention.backup_directory;message('共通の既定値を読み込みました。');
    }catch(e){message(e.message);}finally{busy=false;form.querySelector('fieldset').disabled=!loaded;}
  }
  form.onsubmit=async e=>{e.preventDefault();if(busy||!loaded)return;busy=true;
    try{const response=await fetch(root+'api/operations-defaults',{method:'PUT',headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:JSON.stringify({expected_revision:revision,retention:{enabled:$('#default-retention-enabled').checked,retention_days:Number($('#default-retention-days').value),backup_directory:$('#default-backup-directory').value}})});
      const value=await response.json();if(!response.ok)throw Error(value.detail||'保存できませんでした。');revision=value.revision;message('共通の既定値を保存しました。共通設定を継承するレーザーに適用されます。個別上書きは維持します。');
    }catch(e){message(e.message);}finally{busy=false;}
  };
  $('#fleet-defaults-reload').onclick=load;load();
})();
