from django.core.urlresolvers import reverse
import logging, traceback
import json
from django.conf import settings

logger = logging.getLogger('ci')

class GitHubException(Exception):
  pass

class GitHubAPI(object):
  _api_url = 'https://api.github.com'
  _github_url = 'https://github.com'
  PENDING = 0
  ERROR = 1
  SUCCESS = 2
  FAILURE = 3
  STATUS = ((PENDING, "pending"),
      (ERROR, "error"),
      (SUCCESS, "success"),
      (FAILURE, "failure"),
      )

  def sign_in_url(self):
    return reverse('ci:github:sign_in')

  def user_url(self):
    return "%s/user" % self._api_url

  def repos_url(self, affiliation=None):
    base = "%s/user/repos" % self._api_url
    if affiliation:
      return "%s?affiliation=%s" % (base, affiliation)
    return base

  def orgs_url(self):
    return "%s/user/orgs" % self._api_url

  def org_repo_url(self, org):
    return "%s/orgs/%s/repos" % (self._api_url, org)

  def repo_url(self, owner, repo):
    return "%s/repos/%s/%s" % (self._api_url, owner, repo)

  def status_url(self, owner, repo, sha):
    return "%s/statuses/%s" % (self.repo_url(owner, repo), sha)

  def branches_url(self, owner, repo):
    return "%s/branches" % (self.repo_url(owner, repo))

  def branch_url(self, owner, repo, branch):
    return "%s/%s" % (self.branches_url(owner, repo), branch)

  def repo_html_url(self, owner, repo):
    return "%s/%s/%s" %(self._github_url, owner, repo)

  def commit_comment_url(self, owner, repo, sha):
    return "%s/commits/%s/comments" % (self.repo_url(owner, repo), sha)

  def commit_url(self, owner, repo, sha):
    return "%s/commits/%s" % (self.repo_url(owner, repo), sha)

  def commit_html_url(self, owner, repo, sha):
    return "%s/commits/%s" % (self.repo_html_url(owner, repo), sha)

  def collaborator_url(self, owner, repo, user):
    return "%s/collaborators/%s" % (self.repo_url(owner, repo), user)

  def status_str(self, status):
    for status_pair in self.STATUS:
      if status == status_pair[0]:
        return status_pair[1]
    return None

  def get_repos(self, auth_session, session):
    if 'github_repos' in session:
      return session['github_repos']
    response = auth_session.get(self.repos_url(affiliation='owner'))
    data = self.get_all_pages(auth_session, response)
    owner_repo = []
    if 'message' not in data:
      for repo in data:
        owner_repo.append(repo['name'])
      session['github_repos'] = owner_repo
    return owner_repo

  def get_branches(self, auth_session, owner, repo):
    response = auth_session.get(self.branches_url(owner, repo))
    data = self.get_all_pages(auth_session, response)
    branches = []
    if 'message' not in data:
      for branch in data:
        branches.append(branch['name'])
    return branches

  def get_org_repos(self, auth_session, session):
    if 'github_org_repos' in session:
      return session['github_org_repos']
    response = auth_session.get(self.repos_url(affiliation='organization_member'))
    data = response.json()
    org_repo = []
    if 'message' not in data:
      for repo in data:
        org_repo.append("%s/%s" % (repo['owner']['login'], repo['name']))
      session['github_org_repos'] = org_repo
    return org_repo

  def update_pr_status(self, oauth_session, base, head, state, event_url, description, context):
    if settings.NO_REMOTE_UPDATE:
      return

    data = {
        'state': self.status_str(state),
        'target_url': event_url,
        'description': description,
        'context': context,
        }
    url = self.status_url(base.user().name, base.repo().name, head.sha)
    try:
      response = oauth_session.post(url, data=json.dumps(data))
      if 'updated_at' not in response.content:
        logger.warning("Error setting pr status %s\nSent data: %s\nReply: %s" % (url, data, response.content))
      else:
        logger.info("Set pr status %s" % url)
    except Exception as e:
      logger.warning("Error setting pr status %s\nSent data: %s\nError : %s" \
          % (url, data, traceback.format_exc(e)))

  def is_collaborator(self, oauth_session, user, repo):
    # first just check to see if the user is the owner
    if repo.user == user:
      return True
    # now ask github
    url = self.collaborator_url(repo.user.name, repo.name, user.name)
    logger.debug('Checking %s' % url)
    response = oauth_session.get(url)
    # on success a 204 no content
    if response.status_code != 204:
      logger.debug('User %s is not a collaborator on %s' % (user, repo))
      return False
    logger.debug('User %s is a collaborator on %s' % (user, repo))
    return True

  def pr_comment(self, oauth_session, url, msg):
    if settings.NO_REMOTE_UPDATE:
      return

    comment = {'body': msg}
    try:
      oauth_session.post(url, data=json.dumps(comment))
    except Exception as e:
      logger.warning("Failed to leave comment.\nComment: %s\nError: %s" %(msg, traceback.format_exc(e)))

  def last_sha(self, oauth_session, owner, repo, branch):
    url = self.branch_url(owner, repo, branch)
    try:
      response = oauth_session.get(url)
      if 'commit' in response.content:
        data = json.loads(response.content)
        return data['commit']['sha']
      logger.warning("Unknown branch information for %s\nResponse: %s" % (url, response.content))
    except Exception as e:
      logger.warning("Failed to get branch information at %s.\nError: %s" % (url, traceback.format_exc(e)))
    return None

  def get_all_pages(self, oauth_session, response):
    all_json = response.json()
    while 'next' in response.links:
      response = oauth_session.get(response.links['next']['url'])
      all_json.extends(response.json())
    return all_json

  def install_webhooks(self, request, auth_session, user, repo):
    hook_url = '%s/hooks' % self.repo_url(repo.user.name, repo.name)
    callback_url = request.build_absolute_uri(reverse('ci:github:webhook', args=[user.build_key]))
    response = auth_session.get(hook_url)
    data = self.get_all_pages(auth_session, response)
    have_hook = False
    for hook in data:
      if 'pull_request' not in hook['events'] or 'push' not in hook['events']:
        continue
      if hook['config']['url'] == callback_url and hook['config']['content_type'] == 'json':
        have_hook = True
        break

    if have_hook:
      return None

    add_hook = {
        'name': 'web', # "web" is required for webhook
        'active': True,
        'events': ['push', 'pull_request'],
        'config': {
          'url': callback_url,
          'content_type': 'json',
          }
        }
    response = auth_session.post(hook_url, data=json.dumps(add_hook))
    data = response.json()
    if 'errors' in data:
      raise GitHubException(data['errors'])
    logger.debug('Added webhook to %s for user %s' % (repo, user.name))
