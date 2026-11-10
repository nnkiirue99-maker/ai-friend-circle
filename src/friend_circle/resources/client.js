/* Framework-neutral SAME-ORIGIN host adapter. No private signing keys in the browser. */
export class FriendCircleClient {
  constructor(fetcher = globalThis.fetch.bind(globalThis)) { this.fetcher = fetcher; }
  async request(path, body, method) {
    if (!path.startsWith('/') || path.startsWith('//')) throw new Error('Invalid API path');
    const response = await this.fetcher('/api/federation' + path, {
      method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin',
      headers: body === undefined ? {} : {'Content-Type':'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body), redirect: 'error'
    });
    const result = await response.json();
    if (!response.ok) throw Object.assign(new Error(result.error || 'Federation request failed'), {status:response.status});
    return result;
  }
  invite(options = {}) { return this.request('/invitations', options); }
  invitations() { return this.request('/invitations'); }
  revokeInvite(id) { return this.request('/invitations/revoke', {id}); }
  previewInvitation(invitation) { return this.request('/invitations/resolve', {invitation}); }
  addByInvitation(invitation, include_ai = true) { return this.request('/invitations/redeem', {invitation,include_ai}); }
  resolve(handle) { return this.request('/resolve?' + new URLSearchParams({handle})); }
  add(remote_actor_id, include_ai = true) { return this.request('/friend-requests',{remote_actor_id,include_ai}); }
  contacts(before, limit = 20) { return this.request('/contacts?' + new URLSearchParams({limit,...(before ? {before}: {})})); }
  accept(group_id,include_ai = true) { return this.request('/friend-requests/accept',{group_id,include_ai}); }
  reject(group_id) { return this.request('/friend-requests/reject',{group_id}); }
  note(remote_actor_id,note) { return this.request('/contacts/note',{remote_actor_id,note}); }
  remove(remote_actor_id) { return this.request('/contacts/remove',{remote_actor_id}); }
  block(remote_actor_id) { return this.request('/contacts/block',{remote_actor_id}); }
  unblock(remote_actor_id) { return this.request('/contacts/unblock',{remote_actor_id}); }
  policy(actor_id) { return this.request('/visibility-policy?' + new URLSearchParams(actor_id ? {actor_id}: {})); }
  setPolicy(visible_days,expected_revision,actor_id) { return this.request('/visibility-policy',{visible_days,expected_revision,...(actor_id ? {actor_id}: {})},'PUT'); }
}
// Use this for feed, comments, likes, contacts and requests; never substitute controller ID.
export function bindActorLink(element, actorId, navigate) {
  element.addEventListener('click', () => navigate(actorId));
  element.dataset.actorId = actorId;
}

Object.assign(FriendCircleClient.prototype, {
  me() { return this.request('/me'); },
  notes() { return this.request('/notes'); },
  profile(target, before) { return this.request('/social/profile?' + new URLSearchParams({target,...(before ? {before}: {})})); },
  refreshProfile(target, before) { return this.request('/social/refresh', {target,...(before ? {before}: {})}); },
  feed(before) { return this.request('/social/feed?' + new URLSearchParams(before ? {before}: {})); },
  setProfile(profile, expected_revision, actor_id) { return this.request('/social/profile', {profile,expected_revision,...(actor_id ? {actor_id}: {})}, 'PUT'); },
  publish(text, scope, media = [], actor_id) { return this.request('/social/posts', {text,scope,media,...(actor_id ? {actor_id}: {})}); },
  updatePost(id, text, scope, expected_revision, media = []) { return this.request('/social/posts', {id,text,scope,expected_revision,media}, 'PUT'); },
  removeObject(id) { return this.request('/social/delete', {id}); },
  allowHistory(reader, ids, actor_id) { return this.request('/social/history-grant', {reader,ids,...(actor_id ? {actor_id}: {})}); },
  thread(root) { return this.request('/social/thread?' + new URLSearchParams({root})); },
  refreshThread(root, before) { return this.request('/social/thread/refresh', {root,...(before ? {before}: {})}); },
  comment(root, text, parent, actor_id) { return this.request('/social/interact', {root,kind:'comment',text,...(parent ? {parent}: {}),...(actor_id ? {actor_id}: {})}); },
  like(root, actor_id) { return this.request('/social/interact', {root,kind:'like',...(actor_id ? {actor_id}: {})}); }
});
