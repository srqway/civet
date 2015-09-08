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
    try:
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
    except Exception as e:
      err_str = "Error while creating GitCommitData: %s" % traceback.format_exc(e)
      logger.error(err_str)
      raise models.DBException(err_str)

def get_status(status):
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
  status = set()
  for step_result in job.step_results.all():
      status.add(step_result.status)
  return get_status(status)

def event_status(event):
  status = set()
  for job in event.jobs.all():
    jstatus = job_status(job)
    if jstatus == models.JobStatus.FAILED:
      if job.recipe.abort_on_failure:
        status.add(jstatus)
      else:
        status.add(models.JobStatus.FAILED_OK)
    else:
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

def make_jobs_ready(event):
  status = event_status(event)
  completed_jobs = event.jobs.filter(complete=True)

  if event.jobs.count() == completed_jobs.count():
    event.complete = True
    event.save()
    logger.debug("Event %s complete" % event)
    return

  if status == models.JobStatus.FAILED or status == models.JobStatus.CANCELED:
    for job in event.jobs.filter(complete=False):
      job.ready = False
      job.save()
    return

  completed_set = set(completed_jobs)
  for job in event.jobs.filter(active=True).all():
    recipe_deps = job.recipe.dependencies
    ready = True
    for dep in recipe_deps.all():
      recipe_jobs = set(dep.jobs.filter(event=event).all())
      if not recipe_jobs.issubset(completed_set):
        ready = False
        logger.debug('job {} does not have depends met'.format(job))
        break

    if ready:
      logger.info("Setting job to ready for job %s" % job.recipe)

    if job.ready != ready:
      job.ready = ready
      job.save()


class ManualEvent(object):
  def __init__(self, user, branch, latest):
    self.user = user
    self.branch = branch
    self.latest = latest

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

    recipes = models.Recipe.objects.filter(branch=base.branch, cause=models.Recipe.CAUSE_MANUAL).all()
    if not recipes:
      logger.info("No recipes for manual on %s" % base.branch)
      return

    ev, created = models.Event.objects.get_or_create(
        build_user=self.user,
        head=base,
        base=base,
        cause=models.Event.MANUAL,
        )
    ev.complete = False
    ev.save()

    if created:
      logger.info("Created manual event for %s" % self.branch)

    self._process_recipes(ev, recipes)

  def _process_recipes(self, ev, recipes):
    for recipe in recipes:
      for config in recipe.build_configs.all():
        job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
        job.ready = False
        job.complete = False
        job.active = recipe.active
        job.status = models.JobStatus.NOT_STARTED
        if created:
          job.step_results.all().delete()
        job.save()
    make_jobs_ready(ev)

class PushEvent(object):
  """
  Holds all the data that will go into a Event of
  a Push type. Will create and save the DB tables.
  """
  def __init__(self):
    self.base_commit = None
    self.head_commit = None
    self.comments_url = None
    self.full_text = None
    self.build_user = None

  def save(self, request):
    base = self.base_commit.create()
    head = self.head_commit.create()

    logger.info("New push event on %s" % base.branch)
    recipes = models.Recipe.objects.filter(branch=base.branch, cause=models.Recipe.CAUSE_PUSH).all()
    if not recipes:
      logger.info("No recipes for push on %s" % base.branch)
      return

    ev, created = models.Event.objects.get_or_create(
        build_user=self.build_user,
        head=head,
        base=base,
        complete=False,
        cause=models.Event.PUSH,
        )

    ev.comments_url = self.comments_url
    ev.json_data = json.dumps(self.full_text, indent=4)
    ev.save()
    self._process_recipes(ev, recipes)

  def _process_recipes(self, ev, recipes):
    for recipe in recipes:
      if not recipe.active:
        continue
      for config in recipe.build_configs.all():
        job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
        job.active = True
        if recipe.automatic == recipe.MANUAL:
          job.active = False
        job.ready = False
        job.complete = False
        job.save()
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

  def _already_exists(self, base, head):
    try:
      pr = models.PullRequest.objects.get(
              number=self.pr_number,
              repository=base.branch.repository)
    except models.PullRequest.DoesNotExist:
      return

    if self.action == self.CLOSED and not pr.closed:
      pr.closed = True
      logger.info("Closed pull request %s on %s" % (pr, base.branch))
      pr.save()
    elif self.action == self.REOPENED:
      logger.info("Reopened pull request %s on %s" % (pr, base.branch))
      pr.closed = False
      pr.save()

  def _create_new_pr(self, base, head):
    logger.info("New pull request event %s on %s" % (self.pr_number, base.branch))
    recipes = models.Recipe.objects.filter(repository=base.branch.repository, cause=models.Recipe.CAUSE_PULL_REQUEST).all()
    if not recipes:
      logger.info("No recipes for pull requests on %s" % base.branch)
      return None, None, None


    pr, created = models.PullRequest.objects.get_or_create(
        number=self.pr_number,
        repository=base.branch.repository,
        )
    pr.title=self.title
    pr.closed=False
    pr.url = self.html_url
    pr.save()

    ev, created = models.Event.objects.get_or_create(
        build_user=self.build_user,
        head=head,
        base=base,
        )
    ev.complete = False
    ev.cause = models.Event.PULL_REQUEST
    ev.comments_url = self.comments_url
    ev.pull_request = pr
    ev.json_data = json.dumps(self.full_text, indent=4)
    ev.save()
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

    msg = 'Waiting'
    if not active:
      msg = 'Developer needed'
    for config in recipe.build_configs.all():
      job, created = models.Job.objects.get_or_create(recipe=recipe, event=ev, config=config)
      job.active = active
      job.ready = False
      job.complete = False
      job.save()
      if created:
        logger.debug("Created job %s" % job)

      server.api().update_pr_status(
              oauth_session,
              ev.base,
              ev.head,
              server.api().PENDING,
              request.build_absolute_uri(reverse('ci:view_job', args=[job.pk])),
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
    user = pr.repository.user
    server = user.server
    oauth_session = server.auth().start_session_for_user(user)
    for recipe in recipes:
      self._check_recipe(request, oauth_session, user, pr, ev, recipe)

  def save(self, requests):
    base = self.base_commit.create()
    head = self.head_commit.create()

    if self.action in [self.CLOSED, self.REOPENED]:
      self._already_exists(base, head)
      return

    if self.action in [self.OPENED, self.SYNCHRONIZE]:
      pr, ev, recipes = self._create_new_pr(base, head)
      if not pr:
        return

      try:
        self._process_recipes(requests, pr, ev, recipes)
        make_jobs_ready(ev)
      except Exception as e:
        logger.warning('Error occurred: %s' % traceback.format_exc(e))

