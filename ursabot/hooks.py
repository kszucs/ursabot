# from twisted.python import log
from twisted.internet import defer

from buildbot.www.hooks.github import GitHubEventHandler
from buildbot.util.httpclientservice import HTTPClientService


# rename it to listener
class GithubHook(GitHubEventHandler):

    def _get_github_client(self):
        headers = {'User-Agent': 'Buildbot'}
        if self._token:
            headers['Authorization'] = 'token ' + self._token

        return HTTPClientService.getService(
            self.master, self.github_api_endpoint, headers=headers,
            debug=self.debug, verify=self.verify)

    @defer.inlineCallbacks
    def handle_issue_comment(self, payload, event):
        body = payload['comment']['body']

        if body.startswith('@ursabot '):
            response = 'Good command!'
        else:
            response = 'Wrong command, start with @ursabot!'

        client = self._get_github_client()
        url = payload['issue']['comments_url']
        data = {'body': response}
        yield client.post(url, data=data)
