self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('push',e=>{let d={};try{d=e.data.json()}catch(x){d={title:'黃鼎彥的財富密碼',body:e.data&&e.data.text()}}
  e.waitUntil(self.registration.showNotification(d.title||'黃鼎彥的財富密碼',{body:d.body,icon:'/static/icon-192.png',badge:'/static/icon-192.png',tag:'sig-'+Date.now(),vibrate:[300,100,300],requireInteraction:true}))});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(self.clients.matchAll({type:'window'}).then(cs=>{for(const c of cs)if('focus' in c)return c.focus();return self.clients.openWindow('/')}))});
