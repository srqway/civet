import models
import logging
from django.core.urlresolvers import reverse
logger = logging.getLogger('ci')
import traceback
import json

class GitCommitData(object):
  """
  Creates or gets the required DB tables for a
  GitCommit
  """

  def __init__(self, owner, repo, ref, sha, ssh_url, server):
    self.owner = owner
    self.server = server
    self.repo = repo
    self.ref = ref
    self.sha = sha
    self.ssh_url = ssh_url

  def create(self):
    user, created = models.GitUser.objects.get_or_create(name=self.owner, server=self.server)
    if created:
      logger.info("Created %s user %s:%s" % (self.server.name, user.name, user.build_key))

    repo, created = models.Repository.objects.get_or_create(user=user, name=self.repo)
    if created:
      logger.info("Created %s repo %s" % (self.server.name, str(repo)))

    branch, created = models.Branch.objects.get_or_create(repository=repo, name=self.ref)
    if created:
      logger.info("Created %s branch %s" % (self.server.name, str(branch)))

    commit, created = models.Commit.objects.get_or_create(branch=branch, sha=self.sha)
    if created:
      logger.info("Created %s commit %s" % (self.server.name, str(commit)))

    if not commit.ssh_url and self.ssh_url:
      commit.ssh_url = self.ssh_url
      commit.save()

    return commit

def get_status(status):
  """
  A ordered list of prefered preferences to set.
  """
  if models.JobStatus.FAILED in status:
    return models.JobStatus.FAILED
  if models.JobStatus.CANCELED in status:
    return models.JobStatus.CANCELED
  if models.JobStatus.FAILED_OK in status:
    return models.JobStatus.FAILED_OK
  if models.JobStatus.RUNNING in status:
    return models.JobStatus.RUNNING
  if models.JobStatus.NOT_STARTED in status:
    return models.JobStatus.NOT_STARTED
  if models.JobStatus.SUCCESS in status:
    return models.JobStatus.SUCCESS
  return models.JobStatus.SUCCESS

def job_status(job):
  """
  Figure out what the overall status of a job is.
  """
  status = set()
  for step_result in job.step_results.all():
    status.add(step_result.status)
  job_status = get_status(status)
  return job_status

def event_status(event):
  """
  Figure out what the overall status of an event is.
  """
  status = set()
  for job in event.jobs.all():
    jstatus = job_status(job)
    status.add(jstatus)
  return get_status(status)

def pr_status_update(event, state, context, url, desc):
  event.head.server().api().update_pr_status(
      event.base.repo(),
      event.head.sha,
      state,
      url,
      desc,
      context,
      )

def cancel_event(ev):
  logger.info('Canceling event {}: {}'.format(ev.pk, ev))
  for job in ev.jobs.all():
    if not job.complete:
      job.status = models.JobStatus.CANCELED
      job.complete = True
      job.save()
      logger.info('Canceling event {}: {} : job {}: {}'.format(ev.pk, ev, job.pk, job))
  ev.complete = True
  ev.status = models.JobStatus.CANCELED
  ev.save()


def make_jobs_ready(event):
  status = event_status(event)
  completed_jobs = event.jobs.filter(complete=True)

  if event.jobs.count() == completed_jobs.count():
    event.complete = True
    event.save()
    logger.info('Event {}: {} complete'.format(event.pk, event))
    return

  if status == models.JobStatus.FAILED or status == models.JobStatus.CANCELED:
    # if there is a failed job or it is canceled, don't schedule more jobs
    return

  completed_set = set(completed_jobs)
  for job in event.jobs.filter(active=True).all():
    recipe_deps = job.recipe.dependencies
    ready = True
    for dep in recipe_deps.all():
      recipe_jobs = set(dep.jobs.filter(event=event).all())
      if not recipe_jobs.issubset(completed_set):
        ready = False
        logger.info('job {}: {} does not have depends met'.format(job.pk, job))
        break

    if job.ready != ready:
      job.ready = ready
      job.save()
      logger.info('Job {}: {} : ready: {} : on {}'.format(job.pk, job, job.ready, job.recipe.repository))


class ManualEvent(object):
  def __init__(self, build_user, branch, latest):
    self.user = build_user
    self.branch = branch
    self.latest = latest
    self.description = ''

  def save(self, request):
    base_commit = GitCommitData(
        self.branch.repository.user.name,
        self.branch.repository.name,
        self.branch.name,
        self.latest,
        "",
        self.branch.repository.user.server,
        )
    base = base_commit.create()

    recipes = models.Recipe.objects.filter(active=True, creator=self.user, branch=base.branch, cause=models.Recipe.CAUSE_MANUAL).order_by('-priority', 'display_name').all()
    if not recipes:
      logger.info("No recipes for manual on %s" % base.branch)
      return

    ev, created = models.Event.objects.get_or_create(
        build_user=self.user,
        head=base,
        base=base,
        cause=models.Event.MANUAL,
        )
    if created:
      ev.complete = False
      ev.description = '(scheduled)'
      ev.save()
      logger.info("Created manual event for %s" % self.branch)

    self._process_recipes(ev, recipes)

  def _process_recipes(self, ev, recipes):
    for recipe in recipes:
      for config in recipe.build_configs.all():
        job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
        if created:
          job.ready = False
          job.complete = False
          job.active = recipe.active
          job.status = models.JobStatus.NOT_STARTED
          job.save()
          logger.info('Created job {}: {} on {}'.format(job.pk, job, recipe.repository))
    make_jobs_ready(ev)

class PushEvent(object):
  """
  Holds all the data that will go into a Event of
  a Push type. Will create and save the DB tables.
  self.base_commit : GitCommitData of the base sha
  self.head_commit : GitCommitData of the head sha
  self.comments_url : Url to the comments
  self.full_text : All the payload data
  self.build_user : GitUser corresponding to the build user
  self.description : Description of the push, ie "Merge commit blablabla"
  """
  def __init__(self):
    self.base_commit = None
    self.head_commit = None
    self.comments_url = None
    self.full_text = None
    self.build_user = None
    self.description = ''

  def save(self, request):

    logger.info('New push event on {}/{}'.format(self.base_commit.repo, self.base_commit.ref))
    recipes = models.Recipe.objects.filter(
        active = True,
        branch__repository__user__server = self.base_commit.server,
        branch__repository__user__name = self.base_commit.owner,
        branch__repository__name = self.base_commit.repo,
        branch__name = self.base_commit.ref,
        creator = self.build_user,
        cause = models.Recipe.CAUSE_PUSH).order_by('-priority', 'display_name').all()
    if not recipes:
      logger.info('No recipes for push on {}/{}'.format(self.base_commit.repo, self.base_commit.ref))
      return

    # create this after so we don't create unnecessary commits
    base = self.base_commit.create()
    head = self.head_commit.create()

    ev, created = models.Event.objects.get_or_create(
        build_user=self.build_user,
        head=head,
        base=base,
        complete=False,
        cause=models.Event.PUSH,
        )

    ev.comments_url = self.comments_url
    #FIXME: should maybe just do this only in DEBUG
    ev.json_data = json.dumps(self.full_text, indent=2)
    ev.description = self.description
    ev.save()
    self._process_recipes(ev, recipes)

  def _process_recipes(self, ev, recipes):
    for recipe in recipes:
      if not recipe.active:
        continue
      for config in recipe.build_configs.all():
        job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
        if created:
          job.active = True
          if recipe.automatic == recipe.MANUAL:
            job.active = False
          job.ready = False
          job.complete = False
          job.save()
          logger.info('Created job {}: {}: on {}'.format(job.pk, job, recipe.repository))
    make_jobs_ready(ev)


class PullRequestEvent(object):
  """
  Hold all the data that will go into a Event of
  a Pull Request type. Will create and save the DB tables.
  """
  OPENED = 0
  CLOSED = 1
  REOPENED = 2
  SYNCHRONIZE = 3

  def __init__(self):
    self.pr_number = None
    self.action = None
    self.build_user = None
    self.base_commit = None
    self.head_commit = None
    self.title = None
    self.html_url = None
    self.full_text = None
    self.comments_url = None
    self.description = ''
    self.trigger_user = ''

  def _already_exists(self, base, head):
    try:
      pr = models.PullRequest.objects.get(
              number=self.pr_number,
              repository=base.branch.repository)
    except models.PullRequest.DoesNotExist:
      return

    if self.action == self.CLOSED and not pr.closed:
      pr.closed = True
      logger.info('Closed pull request {}: {} on {}'.format(pr.pk, pr, base.branch))
      pr.save()

  def _create_new_pr(self, base, head):
    logger.info('New pull request event {} on {}'.format(self.pr_number, base.branch.repository))
    recipes = models.Recipe.objects.filter(active=True, creator=self.build_user, repository=base.branch.repository, cause=models.Recipe.CAUSE_PULL_REQUEST).order_by('-priority', 'display_name').all()
    if not recipes:
      logger.info("No recipes for pull requests on %s" % base.branch.repository)
      return None, None, None


    pr, pr_created = models.PullRequest.objects.get_or_create(
        number=self.pr_number,
        repository=base.branch.repository,
        )
    pr.title = self.title
    pr.closed = False
    pr.url = self.html_url
    pr.save()
    if not pr_created:
      logger.info('Pull request {}: {} already exists'.format(pr.pk, pr))

    ev, ev_created = models.Event.objects.get_or_create(
        build_user=self.build_user,
        head=head,
        base=base,
        )

    ev.complete = False
    ev.cause = models.Event.PULL_REQUEST
    ev.comments_url = self.comments_url
    ev.description = self.description
    ev.trigger_user = self.trigger_user
    ev.pull_request = pr
    ev.json_data = json.dumps(self.full_text, indent=2)
    ev.save()
    if not ev_created:
      logger.info('Event {}: {} : {} already exists'.format(ev.pk, ev.base, ev.head))

    if not pr_created and ev_created:
      # Cancel all the previous events on this pull request
      for old_ev in pr.events.all():
        if ev != old_ev:
          cancel_event(old_ev)

    return pr, ev, recipes

  def _check_recipe(self, request, oauth_session, user, pr, ev, recipe):
    """
    Check if an individual recipe is active for the PR.
    If it is not then set a comment on the PR saying that they
    need to activate the recipe.
    """
    if not recipe.active:
      return
    active = False
    user = pr.repository.user
    server = user.server
    if recipe.automatic == models.Recipe.FULL_AUTO:
      active = True
    elif recipe.automatic == models.Recipe.MANUAL:
      active = False
    elif recipe.automatic == models.Recipe.AUTO_FOR_AUTHORIZED:
      if user in recipe.auto_authorized.all():
        active = True
      else:
        active = server.api().is_collaborator(oauth_session, user, recipe.repository)
      if active:
        logger.info('User {} is allowed to activate recipe: {}: {}'.format(user, recipe.pk, recipe))
      else:
        logger.info('User {} is NOT allowed to activate recipe {}: {}'.format(user, recipe.pk, recipe))

    for config in recipe.build_configs.all():
      job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
      if created:
        job.active = active
        job.ready = False
        job.complete = False
        job.status = models.JobStatus.NOT_STARTED
        job.save()
        logger.info('Created job {}: {}: on {}'.format(job.pk, job, recipe.repository))

        abs_job_url = request.build_absolute_uri(reverse('ci:view_job', args=[job.pk]))
        msg = 'Waiting'
        git_status = server.api().PENDING
        if not active:
          msg = 'Developer needed to activate'
          git_status = server.api().SUCCESS
          comment = 'A build job for {} from recipe {} is waiting for a developer to activate it here: {}'.format(ev.head.sha, recipe.name, abs_job_url)
          server.api().pr_comment(oauth_session, ev.comments_url, comment)

        server.api().update_pr_status(
                oauth_session,
                ev.base,
                ev.head,
                git_status,
                abs_job_url,
                msg,
                str(job),
                )

  def _process_recipes(self, request, pr, ev, recipes):
    """
    Go through the recipes for this PR. Set the
    status for each recipe. If the recipe isn't
    active then a comment is added telling the
    user to activate it manually.
    """
    user = ev.build_user
    server = user.server
    oauth_session = server.auth().start_session_for_user(user)
    for recipe in recipes:
      self._check_recipe(request, oauth_session, user, pr, ev, recipe)

  def save(self, requests):
    base = self.base_commit.create()
    head = self.head_commit.create()

    if self.action == self.CLOSED:
      self._already_exists(base, head)
      return

    if self.action in [self.OPENED, self.SYNCHRONIZE, self.REOPENED]:
      pr, ev, recipes = self._create_new_pr(base, head)
      if not pr:
        return

      try:
        self._process_recipes(requests, pr, ev, recipes)
        make_jobs_ready(ev)
      except Exception as e:
        logger.warning('Error occurred: %s' % traceback.format_exc(e))
