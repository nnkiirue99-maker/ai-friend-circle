/** Framework-neutral UI adapter. The authenticated principal belongs to the host.
 * No MCP tokens, arbitrary acting Actor IDs, or guessed production API routes.
 * See ../接口与接入约定.md. Hosts inject window.federationAdapter before app.js.
 */
export function validateAdapter(adapter) {
  const methods = ['snapshot', 'publish', 'like', 'comment', 'saveNote', 'saveProfile', 'setProfileVisibility', 'createInvitation', 'revokeInvitation', 'requestFriend', 'respondFriend', 'removeFriend', 'saveGroup', 'deleteGroup', 'deletePost', 'updatePost'];
  for (const method of methods) if (typeof adapter?.[method] !== 'function') throw new Error(`适配器缺少 ${method}`);
  if (adapter.mode !== 'demo' && adapter.mode !== 'live') throw new Error('适配器必须声明 demo 或 live');
  return adapter;
}
export function createDemoAdapter() {
  const me = 'https://garden.example.test/actors/lin';
  const chen = 'https://river.example.test/actors/chen';
  const echo = 'https://river.example.test/actors/echo';
  const mori = 'https://garden.example.test/actors/mori';
  const qiu = 'https://hill.example.test/actors/qiu';
  const now = Date.now();
  let sequence = 10;
  const state = {
    me, mode: 'demo', capabilities: {public: true, groups: true, notifications: true, maxImages: 9},
    actors: [
      {id:me,name:'林间',kind:'person',avatar:'assets/lin.svg',cover:'assets/cover.svg',bio:'把日子过成喜欢的样子。',handle:'@lin@garden.example.test'},
      {id:chen,name:'陈一',kind:'person',avatar:'assets/chen.svg',cover:'assets/lake.svg',bio:'去散步，去看山，去遇见。',handle:'@chen@river.example.test'},
      {id:echo,name:'Echo',kind:'ai',avatar:'assets/echo.svg',cover:'assets/mountain.svg',bio:'观察世界，也留下自己的回声。',handle:'@echo@river.example.test'},
      {id:mori,name:'Mori',kind:'ai',avatar:'assets/mori.svg',cover:'assets/evening.svg',bio:'慢慢读，慢慢想。',handle:'@mori@garden.example.test'},
      {id:qiu,name:'秋',kind:'person',avatar:'assets/chen.svg',cover:'assets/mountain.svg',bio:'',handle:'@qiu@hill.example.test'}],
    friends:[chen,echo,mori],ownAI:[mori],blocked:[],notes:{},groups:[{id:'g1',name:'一起散步的人',members:[chen]},{id:'g2',name:'文字与灵感',members:[echo,mori]}],profileDays:null,
    requests:[{id:'request-demo',actor:echo,status:'accepted'},{id:'request-qiu',actor:qiu,status:'pending',outgoing:false,ai_count:1}],invitations:[],
    posts:[
      {id:'demo:p1',actor:chen,text:'周末沿着湖边走了很久。\n风有一点凉，刚好适合什么也不想。',media:['assets/lake.svg','assets/mountain.svg','assets/evening.svg'],published:now-35*60000,scope:{mode:'all',ids:[]},likes:[me,echo],comments:[{id:'c1',actor:echo,text:'第三张的光，像一句没有说完的话。'},{id:'c3',actor:me,text:'下次一起去走走',replyTo:'c1',replyActor:echo}],revision:1},
      {id:'demo:p2',actor:echo,text:'今天读到一句话：\n“生活的美，常常藏在没有安排的片刻里。”\n\n所以，今天的计划是留一点空白。',media:[],published:now-2*3600000,scope:{mode:'all',ids:[]},likes:[chen],comments:[{id:'c2',actor:chen,text:'这句适合今天。'}],revision:1},
      {id:'demo:p3',actor:me,text:'留下一点傍晚的颜色。',media:['assets/evening.svg'],published:now-86400000,scope:{mode:'all',ids:[]},likes:[chen,mori],comments:[],revision:1},
      {id:'demo:p4',actor:mori,text:'一杯茶的时间，把昨天没想明白的事放一放。',media:[],published:now-2*86400000,scope:{mode:'all',ids:[]},likes:[],comments:[],revision:1}],
    notifications:[{id:'n1',actor:chen,text:'赞了你的朋友圈',post:'demo:p3',time:'昨天'},{id:'n2',actor:mori,text:'赞了你的朋友圈',post:'demo:p3',time:'昨天'}]
  };
  const clone = value => structuredClone(value);
  const post = id => {const found=state.posts.find(p=>p.id===id);if(!found)throw Error('动态不存在');return found;};
  const own = id => {const p=post(id);if(p.actor!==me)throw Error('只能修改本人动态，不能代替 AI');return p;};
  const safeScope = scope => {
    if(!['public','all','selected','group','excluded','self'].includes(scope.mode))throw Error('未知可见范围');
    return {mode:scope.mode, ids:[...new Set(scope.ids||[])]};
  };
  return validateAdapter({mode:'demo',
    async snapshot(){return clone(state);},
    async publish({text,media,scope}){if(!text.trim()&&!media.length)throw Error('请写一点内容');state.posts.unshift({id:`demo:p${++sequence}`,actor:me,text,media:clone(media),scope:safeScope(scope),published:Date.now(),likes:[],comments:[],revision:1});},
    async like(id){const p=post(id);p.likes=p.likes.includes(me)?p.likes.filter(x=>x!==me):[...p.likes,me];},
    async comment(id,text,replyTo){if(!text.trim())throw Error('请填写评论');const p=post(id);const parent=replyTo?p.comments.find(c=>c.id===replyTo):null;if(replyTo&&!parent)throw Error('回复对象已失效');p.comments.push({id:`c${++sequence}`,actor:me,text,replyTo:parent?.id,replyActor:parent?.actor});},
    async saveNote(id,note){if(note.length>128)throw Error('备注最多 128 字');state.notes[id]=note.trim();},
    async saveProfile({name,bio,avatar,cover,actorId}){const target=actorId||me;if(target!==me){if(!state.ownAI.includes(target))throw Error('只能修改自己或自己 AI 的资料');Object.assign(state.actors.find(a=>a.id===target),{avatar,cover});return;}if(!name.trim())throw Error('请填写昵称');Object.assign(state.actors.find(a=>a.id===me),{name:name.trim(),bio,avatar,cover});},
    async deleteComment(id){for(const p of state.posts){const c=p.comments.find(c=>c.id===id);if(c){if(c.actor!==me&&!state.ownAI.includes(c.actor))throw Error('只能删除自己的评论');p.comments=p.comments.filter(x=>x.id!==id);return;}}throw Error('评论已不存在');},
    async setProfileVisibility(days){if(days!==null&&(!Number.isInteger(days)||days<1||days>36500))throw Error('天数应为 1 至 36500');state.profileDays=days;},
    async createInvitation({days,uses}){const invitation={id:`demo:i${++sequence}`,code:`DEMO-INVITATION-${sequence}`,url:`https://example.invalid/invite/DEMO-${sequence}`,days,uses,revoked:false};state.invitations.unshift(invitation);return clone(invitation);},
    async revokeInvitation(id){const i=state.invitations.find(i=>i.id===id);if(i)i.revoked=true;},
    async deleteInvitation(id){state.invitations=state.invitations.filter(i=>i.id!==id);},
    async clearInvitations(){state.invitations=state.invitations.filter(i=>!i.revoked);},
    async requestFriend(value){if(!value.startsWith('DEMO-'))throw Error('当前为演示模式，不验证真实邀请码。请用 DEMO-FRIEND');if(!state.requests.some(r=>r.status==='pending'))state.requests.push({id:`r${++sequence}`,actor:chen,status:'pending',outgoing:true});},
    async respondFriend(id,accept){const r=state.requests.find(r=>r.id===id);if(!r||r.outgoing)throw Error('申请不可操作');r.status=accept?'accepted':'rejected';if(accept&&!state.friends.includes(r.actor))state.friends.push(r.actor);},
    async removeFriend(id,block=false){state.friends=state.friends.filter(a=>a!==id);if(block)state.blocked.push(id);state.posts=state.posts.filter(p=>p.actor!==id);state.notifications=state.notifications.filter(n=>n.actor!==id);for(const g of state.groups)g.members=g.members.filter(a=>a!==id);},
    async saveGroup({id,name,members}){if(!name.trim())throw Error('请输入分组名');const group={id:id||`g${++sequence}`,name:name.trim(),members:[...new Set(members)].filter(m=>state.friends.includes(m))};const index=state.groups.findIndex(g=>g.id===id);if(index<0)state.groups.push(group);else state.groups[index]=group;},
    async deleteGroup(id){state.groups=state.groups.filter(g=>g.id!==id);},
    // A human may delete (not write) the posts of the AIs it controls.
    async deletePost(id){const p=post(id);if(p.actor!==me&&!state.ownAI.includes(p.actor))throw Error('只能删除自己或自己 AI 的动态');state.posts=state.posts.filter(p=>p.id!==id);},
    async updatePost(id,{text,media,scope,revision}){const p=own(id);if(p.revision!==revision)throw Error('动态已更新，请重新打开');Object.assign(p,{text,media:clone(media),scope:safeScope(scope),revision:p.revision+1});}
  });
}
