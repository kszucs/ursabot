from urllib.parse import urlparse

from twisted.python import log
from twisted.internet import defer

from buildbot.www.hooks.github import GitHubEventHandler
from buildbot.util.httpclientservice import HTTPClientService


BOTNAME = 'ursabot'


class GithubHook(GitHubEventHandler):

    def _client(self):
        headers = {'User-Agent': 'Buildbot'}
        if self._token:
            headers['Authorization'] = 'token ' + self._token

        # TODO(kszucs): initialize it once?
        return HTTPClientService.getService(
            self.master, self.github_api_endpoint, headers=headers,
            debug=self.debug, verify=self.verify)

    @defer.inlineCallbacks
    def _get(self, url):
        url = urlparse(url)
        client = yield self._client()
        response = yield client.get(url.path)
        result = yield response.json()
        return result

    @defer.inlineCallbacks
    def _post(self, url, data):
        url = urlparse(url)
        client = yield self._client()
        response = yield client.post(url.path, json=data)
        result = yield response.json()
        return result

    def _parse_command(self, message):
        # TODO(kszucs): make it more sophisticated
        mention = f'@{BOTNAME}'
        if mention in message:
            return message.split(mention)[-1].lower().strip()
        return None

    @defer.inlineCallbacks
    def handle_issue_comment(self, payload, event):
        url = payload['issue']['comments_url']
        body = payload['comment']['body']
        repo = payload['repository']
        sender = payload['sender']['login']
        pull_request = payload['issue'].get('pull_request', None)
        command = self._parse_command(body)

        if sender == BOTNAME or command is None:
            # don't respond to itself
            return [], 'git'

        if pull_request is None:
            message = 'Ursabot only listens to pull request comments!'

        changes = []
        if command == 'build':
            try:
                message = "I've started builds for this PR"
                pull_request = yield self._get(pull_request['url'])
                pr_payload = {
                    'action': 'synchronize',
                    'repository': repo,
                    'number': pull_request['number'],
                    'pull_request': pull_request,
                }
                changes, _ = yield self.handle_pull_request(pr_payload, event)
            except Exception as e:
                message = "I've failed to start builds for this PR"
                log.err(f'{message}: {e}')
        else:
            message = f'Unknown command "{command}"'

        log.msg(f'Sending answer "{message}" to {url}')
        result = yield self._post(url, {'body': message})
        log.msg(f'Comment is sent with the following result: {result}')

        return changes, 'git'

    # TODO(kszucs):
    # handle_commit_comment d
    # handle_pull_request_review
    # handle_pull_request_review_comment
