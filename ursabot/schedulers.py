from buildbot.plugins import schedulers, util


class SchedulerMixin:

    def __init__(self, *args, builders, **kwargs):
        builder_names = [b.name for b in builders]
        super().__init__(*args, builderNames=builder_names, **kwargs)


class ForceScheduler(SchedulerMixin, schedulers.ForceScheduler):

    def __init__(self, *args, project, repository, **kwargs):
        """Improves the default form of ForceScheduler."""
        codebase = util.CodebaseParameter(
            codebase='',
            label='',
            project=util.FixedParameter(name='project', default=project),
            repository=util.FixedParameter(name='repository',
                                           default=repository),
            branch=util.StringParameter(name='branch',
                                        default='master',
                                        required=True),
            # required, otherwise status push reporter fails with a
            # non-descriptive exception
            revision=util.StringParameter(name='revision', required=True)
        )
        super().__init__(*args, codebases=[codebase], **kwargs)


class TryScheduler(SchedulerMixin, schedulers.Try_Userpass):
    pass


class AnyBranchScheduler(SchedulerMixin, schedulers.AnyBranchScheduler):
    pass
