'use strict';
// The same value planes as fanlab_devices: observed, draft and confirming.
window.LDConCommittingInput=class {
  constructor(input,{step,sendOnArrows,onCommit,format=String,quantum=1e-9,clock=()=>Date.now()}) {
    Object.assign(this,{input,step,sendOnArrows,onCommit,format,quantum,clock});
    this.last=null;this.draft=null;this.stamp=null;this.state='synced';this.version=0;this.edited=0;this.sent=null;
    input.type='text';input.inputMode='decimal';input.autocomplete='off';this.paint();
    input.addEventListener('input',()=>this.edit());
    input.addEventListener('keydown',e=>{
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();this.revert();}
      else if(e.key==='Enter'){e.preventDefault();e.stopPropagation();onCommit();}
      else if(['ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();if(!e.repeat)this.nudge(e.key==='ArrowUp'?1:-1);}
    });
    input.addEventListener('wheel',e=>{if(document.activeElement!==input)return;e.preventDefault();if(e.deltaY)this.nudge(e.deltaY<0?1:-1);},{passive:false});
  }
  equal(a,b){return Number.isFinite(a)&&Number.isFinite(b)&&Math.abs(a-b)<=this.quantum;}
  rawValue(){return this.input.value.trim()?Number(this.input.value):NaN;}
  value(){return this.draft===null?this.rawValue():this.draft;}
  reformat(){if(this.state==='synced'&&Number.isFinite(this.last))this.draft=this.last;if(Number.isFinite(this.value()))this.input.value=this.format(this.value());}
  paint(){this.input.dataset.syncState=this.state;this.input.title=this.state==='pending'?'Edited — Enter sends edited values; Esc reverts':this.state==='confirming'?'Sent — waiting for confirmed readback':this.state==='rejected'?'Not confirmed — inspect the result before retrying':'';}
  edit(value=this.rawValue()){this.draft=value;this.version++;this.edited=this.clock();this.state=this.equal(this.value(),this.last)?'synced':'pending';this.paint();}
  nudge(sign){if(this.input.disabled)return;let value=this.value();if(!Number.isFinite(value))value=this.last;if(!Number.isFinite(value))return;
    let next;try{next=this.step(value,sign);}catch(error){this.input.setCustomValidity(error.message);this.input.reportValidity();return;}this.input.setCustomValidity('');if(!Number.isFinite(next))return;this.input.value=this.format(next);this.edit(next);if(this.sendOnArrows())this.onCommit([this]);}
  observe(value,stamp){if(!Number.isFinite(value))return;this.last=value;this.stamp=stamp;
    if(this.state==='confirming'){if(this.acknowledged&&this.equal(this.value(),value)){this.state='synced';this.draft=value;this.input.value=this.format(value);}}
    else if(this.state==='synced'){if(document.activeElement!==this.input||this.draft===null){this.draft=value;this.input.value=this.format(value);}}
    else if(this.state==='pending'&&this.equal(this.value(),value)){this.state='synced';}
    this.paint();}
  begin(){this.sent={version:this.version,value:this.value()};this.acknowledged=false;this.state='confirming';this.paint();return this.sent;}
  accepted(sent){if(this.version!==sent.version)return;this.acknowledged=true;this.confirmedAt=this.clock();this.observe(this.last,this.stamp);}
  rejected(sent){if(this.version!==sent.version)return;this.state='rejected';this.edited=this.clock();this.paint();}
  revert(){if(this.state==='confirming')return;this.draft=this.last;this.input.setCustomValidity('');if(Number.isFinite(this.last))this.input.value=this.format(this.last);else this.input.value='';this.state='synced';this.version++;this.paint();}
  expire(now=this.clock(),idleSince=this.edited){if(['pending','rejected'].includes(this.state)&&now-idleSince>=60000&&document.activeElement!==this.input)this.revert();
    if(this.state==='confirming'&&this.acknowledged&&now-this.confirmedAt>5000){this.state='rejected';this.edited=now;this.paint();}}
};
