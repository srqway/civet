from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
import logging, traceback
from ci.github.api import GitHubAPI
import json
from ci import event, models

logger = logging.getLogger('ci')

class GitHubException(Exception):
  pass

def process_push(user, data):
  push_event = event.PushEvent()
  push_event.build_user = user
  push_event.user = data['sender']['login']

  repo_data = data['repository']
  ref = data['ref'].split('/')[-1] # the format is usually of the form "refs/heads/devel"
  push_event.base_commit = event.GitCommitData(
      repo_data['owner']['name'],
      repo_data['name'],
      ref,
      data['before'],
      repo_data['ssh_url'],
      user.server
      )
  push_event.head_commit = event.GitCommitData(
      repo_data['owner']['name'],
      repo_data['name'],
      ref,
      data['after'],
      repo_data['ssh_url'],
      user.server
      )
  url = GitHubAPI().commit_comment_url(repo_data['name'], repo_data['owner']['name'], data['after'])
  push_event.comments_url = url
  push_event.full_text = data
  return push_event

def process_pull_request(user, data):
  pr_event = event.PullRequestEvent()
  pr_data = data['pull_request']

  action = data['action']

  pr_event.pr_number = int(data['number'])
  if action == 'opened' or action == 'synchronize':
    pr_event.action = event.PullRequestEvent.OPENED
  elif action == 'closed':
    pr_event.action = event.PullRequestEvent.CLOSED
  elif action == 'reopened':
    pr_event.action = event.PullRequestEvent.REOPENED
  elif action in ['labeled', 'unlabeled', 'assigned', 'unassigned']:
    # actions that we don't support
    return None
  else:
    raise GitHubException("Pull request %s contained unknown action." % pr_event.pr_number)

  pr_event.build_user = user
  pr_event.comments_url = pr_data['comments_url']
  pr_event.title = pr_data['title']
  pr_event.html_url = pr_data['html_url']

  base_data = pr_data['base']
  pr_event.base_commit = event.GitCommitData(
      base_data['repo']['owner']['login'],
      base_data['repo']['name'],
      base_data['ref'],
      base_data['sha'],
      base_data['repo']['ssh_url'],
      user.server
      )
  head_data = pr_data['head']
  pr_event.head_commit = event.GitCommitData(
      head_data['repo']['owner']['login'],
      head_data['repo']['name'],
      head_data['ref'],
      head_data['sha'],
      head_data['repo']['ssh_url'],
      user.server
      )

  pr_event.full_text = data
  return pr_event

@csrf_exempt
def webhook(request, build_key):
  if request.method != 'POST':
    return HttpResponseNotAllowed(['POST'])

  user = models.GitUser.objects.filter(build_key=build_key).first()
  if not user:
    err_str = "No user with build key %s" % build_key
    logger.warning(err_str)
    return HttpResponseBadRequest(err_str)

  try:
    json_data = json.loads(request.body)
    if 'pull_request' in json_data:
      ev = process_pull_request(user, json_data)
      if ev:
        ev.save(request)
      return HttpResponse('OK')
    elif 'commits' in json_data:
      ev = process_push(user, json_data)
      ev.save(request)
      return HttpResponse('OK')
    elif 'zen' in json_data:
      # this is a ping that gets called when first
      # installing a hook. Just log it and move on.
      logger.info('Got ping for user {}'.format(user.name))
      return HttpResponse('OK')
    else:
      err_str = 'Unknown post to github hook : %s' % request.body
      logger.warning(err_str)
      return HttpResponseBadRequest(err_str)
  except Exception as e:
    err_str ="Invalid call to github/webhook for build key %s. Error: %s" % (build_key, traceback.format_exc(e))
    logger.warning(err_str)
    return HttpResponseBadRequest(err_str)
