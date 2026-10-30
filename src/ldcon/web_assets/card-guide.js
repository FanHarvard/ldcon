'use strict';
(()=>{
  const valid=['en','ja','zh-CN'];
  const detected=()=>navigator.language.startsWith('ja')?'ja':navigator.language.startsWith('zh')?'zh-CN':'en';
  function set(lang,persist=false){lang=valid.includes(lang)?lang:detected();document.documentElement.lang=lang;for(const section of document.querySelectorAll('[data-guide-language]'))section.hidden=section.dataset.guideLanguage!==lang;for(const b of document.querySelectorAll('[data-guide-select]'))b.setAttribute('aria-pressed',String(b.dataset.guideSelect===lang));if(persist)try{localStorage.setItem('ldcon.language',lang);}catch(_){} }
  for(const button of document.querySelectorAll('[data-guide-select]'))button.onclick=()=>set(button.dataset.guideSelect,true);
  window.addEventListener('storage',e=>{if(e.key==='ldcon.language')set(e.newValue);});
  let lang;try{lang=localStorage.getItem('ldcon.language');}catch(_){}set(lang);
})();
