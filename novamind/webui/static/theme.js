(function(){
  'use strict';let applying=false,lastTheme='';
  const stored=localStorage.getItem('novamind-theme');
  document.documentElement.dataset.theme=stored||(matchMedia('(prefers-color-scheme:light)').matches?'light':'dark');
  const workspace=location.pathname==='/'||location.pathname.endsWith('index.html');
  let nativeTheme='';
  function syncNative(){if(!workspace||!window.pywebview?.api?.set_workspace_theme)return;const root=document.documentElement,css=getComputedStyle(root);const theme=root.dataset.theme,background=css.getPropertyValue('--bg').trim(),foreground=css.getPropertyValue('--text').trim(),signature=[theme,background,foreground].join('|');if(signature===nativeTheme)return;window.pywebview.api.set_workspace_theme(theme,background,foreground).then(ok=>{if(ok)nativeTheme=signature;}).catch(()=>{});}
  window.addEventListener('pywebviewready',syncNative);
  async function sync(){try{const data=await NovaMindUI.json('/personal/settings');let theme=data.appearance.theme;
    if(location.pathname.includes('bubble')||location.pathname.includes('quick')){if(data.desktop.theme!=='follow')theme=data.desktop.theme;}
    if(theme==='system')theme=matchMedia('(prefers-color-scheme:light)').matches?'light':'dark';
    if(theme!==lastTheme){applying=true;document.documentElement.dataset.theme=theme;localStorage.setItem('novamind-theme',theme);lastTheme=theme;applying=false;}
    window.dispatchEvent(new CustomEvent('novamind-settings',{detail:data}));
    syncNative();
  }catch(_){}}
  if(workspace)new MutationObserver(()=>{syncNative();if(applying)return;const theme=document.documentElement.dataset.theme;if(theme===lastTheme)return;lastTheme=theme;NovaMindUI.json('/personal/theme',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme})}).catch(()=>{});}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  window.novaMindSyncTheme=sync;
  sync();setInterval(()=>{if(!window.novaMindNativeBubble)sync();},2500);
})();
