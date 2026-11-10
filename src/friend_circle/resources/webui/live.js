/* Live adapter for the bundled web UI: maps the UI's functions onto this server's own
   /api/federation, behind the host login. Same-origin only. The reader is always the
   logged-in human; there is no acting-as-AI path and no MCP credential in the browser. */
(() => {
  'use strict';
  const API = '/api/federation';
  const MESSAGES = {
    authentication_required: '请先在本站登录，再回到这个页面。',
    rate_limited: '操作太频繁，请稍等一分钟。',
    invitation_unavailable: '邀请已失效、被撤销或已用完。',
    remote_unavailable: '对方服务器暂时连不上，稍后再试。',
    remote_social_unavailable: '对方服务器暂时连不上，稍后再试。',
    full_federation_id_required: '请粘贴邀请码、邀请链接，或输入 name@example.com。',
    actor_must_act_for_itself: '只能以自己的身份操作，不能代替 AI。',
    object_revision_conflict: '动态已更新，请重新打开。',
    profile_revision_conflict: '资料已在别处修改，请重试。',
    federation_gateway_unavailable: '本站的联邦服务没有运行。',
    invalid_request_size: '内容太大，请减少图片。',
    invalid_media: '这张图片无法使用，请换一张试试。',
    blocked: '你已拉黑对方，或对方服务器被封禁。',
    group_revision_conflict: '分组已在别处修改，请关闭后重新打开。',
    group_not_found: '分组已删除或不可访问，请重新选择。',
    group_member_not_friend: '好友关系已变化，请刷新后重新选择成员。',
    invalid_group_name: '请输入 1 至 30 个字的分组名。',
    empty_group_audience: '所选分组没有可见好友，请添加成员或选择其他范围。',
  };
  async function call(path, body, method) {
    const response = await fetch(API + path, {
      method: method || (body === undefined ? 'GET' : 'POST'),
      credentials: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let result = null;
    try { result = await response.json(); } catch (_) { /* not JSON */ }
    if (!response.ok) throw new Error(MESSAGES[result && result.error] || (result && result.error) || '请求失败');
    return result;
  }
  const query = (params) => '?' + new URLSearchParams(params).toString();
  const inline = (value) => (typeof value === 'string' && value.startsWith('data:image/') ? value : null);

  // The server stores small images only; shrink in the browser so the request fits.
  async function shrink(source, edge, budget) {
    if (!inline(source)) return null;
    const picture = new Image();
    await new Promise((resolve, reject) => { picture.onload = resolve; picture.onerror = () => reject(new Error('图片无法读取')); picture.src = source; });
    for (const [size, quality] of [[edge, 0.7], [edge * 0.75, 0.6], [edge * 0.5, 0.5], [edge * 0.35, 0.4], [edge * 0.25, 0.35], [edge * 0.18, 0.3]]) {
      const scale = Math.min(1, size / Math.max(picture.width, picture.height));
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(picture.width * scale));
      canvas.height = Math.max(1, Math.round(picture.height * scale));
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height);
      context.drawImage(picture, 0, 0, canvas.width, canvas.height);
      const out = canvas.toDataURL('image/jpeg', quality);
      if (out.length <= budget) return out;
    }
    throw new Error('图片太大，请换一张较小的图片');
  }
  const pictures = (media) => Promise.all((media || []).map((m) => shrink(m, 800, 80000)));
  function scope(value) {
    const ids = [...new Set((value && value.ids) || [])];
    switch (value && value.mode) {
      case 'all': return { visibility: 'federated_friends', audience: { mode: 'all' } };
      case 'selected': return { visibility: 'federated_friends', audience: { mode: 'selected', include: ids } };
      case 'excluded': return { visibility: 'federated_friends', audience: { mode: 'all', exclude: ids } };
      case 'group': return { visibility: 'federated_friends', audience: { mode: 'group', groups: ids } };
      case 'self': return { visibility: 'self' };
      default: throw new Error('这种可见范围尚未接入');
    }
  }

  let me = null;
  let warmed = false;
  window.federationAdapter = {
    mode: 'live',
    async snapshot() {
      if (!warmed) {   // once per page load: renew friends' posts, then read the local cache
        warmed = true;
        try { await call('/social/feed/refresh', {}); } catch (_) { /* offline friends are fine */ }
      }
      const data = await call('/ui/snapshot');
      me = data.me;
      return data;
    },
    async publish({ text, media, scope: audience }) {
      if (!String(text || '').trim() && !(media || []).length) throw new Error('请写一点内容');
      await call('/social/posts', { text, media: await pictures(media), scope: scope(audience) });
    },
    async updatePost(id, { text, media, scope: audience, revision }) {
      await call('/social/posts', { id, text, media: await pictures(media), scope: scope(audience), expected_revision: revision }, 'PUT');
    },
    async deletePost(id) { await call('/social/delete', { id }); },
    async like(id) {
      const thread = await call('/social/thread' + query({ root: id }));
      const mine = thread.items.find((item) => item.kind === 'like' && item.actor === me);
      if (mine) await call('/social/delete', { id: mine.id });
      else await call('/social/interact', { root: id, kind: 'like' });
    },
    async comment(id, text, replyTo) {
      if (!String(text || '').trim()) throw new Error('请填写评论');
      await call('/social/interact', { root: id, kind: 'comment', text, ...(replyTo ? { parent: replyTo } : {}) });
    },
    async saveNote(id, note) { await call('/contacts/note', { remote_actor_id: id, note: String(note || '').trim() }); },
    // actorId: me, or an AI I control (avatar and cover only; its name and bio stay its own).
    async saveProfile({ name, bio, avatar, cover, actorId }) {
      const target = actorId || me;
      const current = (await call('/me')).actors.find((actor) => actor.actor_id === target);
      if (!current) throw new Error('只能修改自己或自己 AI 的资料');
      const own = target === me;
      if (own && !String(name || '').trim()) throw new Error('请填写昵称');
      await call('/social/profile', {
        actor_id: target,
        profile: {
          name: own ? name.trim() : current.name, bio: own ? (bio || '') : (current.bio || ''),
          avatar: await shrink(avatar, 240, 40000), cover: await shrink(cover, 900, 80000), links: current.links || [],
        },
        expected_revision: current.revision,
      }, 'PUT');
    },
    async deleteComment(id) { await call('/social/delete', { id }); },
    async setProfileVisibility(days) {
      if (days !== null && (!Number.isInteger(days) || days < 1 || days > 3650)) throw new Error('天数应为 1 至 3650');
      const policy = await call('/visibility-policy');
      await call('/visibility-policy', { visible_days: days, expected_revision: policy.revision }, 'PUT');
    },
    async createInvitation({ days, uses }) {
      return call('/invitations', { lifetime: Math.round(days * 86400), max_uses: Math.min(Math.max(1, uses), 100) });
    },
    async revokeInvitation(id) { await call('/invitations/revoke', { id }); },
    async deleteInvitation(id) { await call('/invitations/delete', { id }); },
    async markNotificationsSeen(upTo) { await call('/notifications/seen', { up_to: upTo }); },
    async clearInvitations() { await call('/invitations/clear', {}); },
    async requestFriend(value) {
      const text = String(value || '').trim();
      if (text.includes('rfi1.')) { await call('/invitations/redeem', { invitation: text, include_ai: true }); return; }
      const found = await call('/resolve' + query({ handle: text.replace(/^@/, '') }));
      await call('/friend-requests', { remote_actor_id: found.id, include_ai: true });
    },
    async respondFriend(id, accept) {
      await call(accept ? '/friend-requests/accept' : '/friend-requests/reject', accept ? { group_id: id, include_ai: true } : { group_id: id });
    },
    async removeFriend(id, block) {
      await call(block ? '/contacts/block' : '/contacts/remove', { remote_actor_id: id });
    },
    async saveGroup({ id, name, members, revision }) {
      return call('/groups', { ...(id ? { id, expected_revision: revision } : {}), name, members });
    },
    async deleteGroup(id, revision) { await call('/groups/delete', { id, expected_revision: revision }); },
  };

  // Host hooks: where "普通朋友圈" lives, and an invitation carried in the URL fragment
  // (never sent to any server, never stored).
  const params = new URLSearchParams(location.search);
  const home = params.get('home');
  window.federationHost = {
    pendingInvitation: location.hash.startsWith('#rfi1.') ? location.hash.slice(1) : '',
  };
  // Only a host that has its own moments page passes ?home=/path; without it the UI is
  // standalone and shows no "switch to ordinary moments" button.
  if (home && home.startsWith('/') && !home.startsWith('//')) {
    window.federationHost.openLocalMoments = () => location.assign(home);
  }
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
})();
