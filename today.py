import requests
import os
from lxml import etree
import time
import hashlib

# Self-updating GitHub profile stats. Ported from Andrew6rant/Andrew6rant.
# Fine-grained personal access token with All Repositories access:
#   Account permissions:    read:Followers, read:Starring, read:Watching
#   Repository permissions: read:Commit statuses, read:Contents, read:Metadata, read:Pull Requests
ACCESS_TOKEN = os.environ.get('ACCESS_TOKEN', '')
USER_NAME = os.environ.get('USER_NAME', 'unmeshlele')
HEADERS = {'authorization': 'token ' + ACCESS_TOKEN} if ACCESS_TOKEN else {}
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def simple_request(func_name, query, variables):
    """Returns a request, or raises an Exception if the response does not succeed."""
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """Uses GitHub's GraphQL v4 API to return total commit count."""
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar { totalContributions }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    res_data = request.json()
    user_data = res_data.get('data', {}).get('user')
    if user_data and user_data.get('contributionsCollection'):
        calendar = user_data['contributionsCollection'].get('contributionCalendar')
        if calendar and 'totalContributions' in calendar:
            return int(calendar['totalContributions'])
    return 0


def graph_repos_stars(count_type, owner_affiliation, cursor=None, edges=None):
    """Uses GitHub's GraphQL v4 API to return total repository or star count."""
    if edges is None:
        edges = []
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges { node { ... on Repository { nameWithOwner stargazers { totalCount } } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    res_data = request.json().get('data', {}).get('user', {}).get('repositories', {})
    if count_type == 'repos':
        return res_data.get('totalCount', 0)
    elif count_type == 'stars':
        current_edges = res_data.get('edges') or []
        edges.extend(current_edges)
        if res_data.get('pageInfo', {}).get('hasNextPage'):
            return graph_repos_stars(count_type, owner_affiliation, res_data['pageInfo']['endCursor'], edges)
        return stars_counter(edges)


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """Uses cursor pagination to fetch 100 commits from a repository at a time."""
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef { target { ... on Commit {
                history(first: 100, after: $cursor) {
                    totalCount
                    edges { node { ... on Commit { committedDate } author { user { id } } deletions additions } }
                    pageInfo { endCursor hasNextPage }
                }
            } } }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        repo_data = request.json().get('data', {}).get('repository')
        if repo_data and repo_data.get('defaultBranchRef') and repo_data['defaultBranchRef'].get('target'):
            history = repo_data['defaultBranchRef']['target'].get('history')
            if history:
                return loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits)
        return addition_total, deletion_total, my_commits
    force_close_file(data, cache_comment)
    if request.status_code == 403:
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')
    raise Exception('recursive_loc() has failed with a', request.status_code, request.text, QUERY_COUNT)


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """Only adds the LOC of commits authored by me."""
    for node in history.get('edges', []):
        if not node or not isinstance(node, dict) or not node.get('node'):
            continue
        commit_node = node['node']
        author = commit_node.get('author')
        if author and isinstance(author, dict):
            user = author.get('user')
            if user and user == OWNER_ID:
                my_commits += 1
                addition_total += commit_node.get('additions', 0)
                deletion_total += commit_node.get('deletions', 0)
    if not history.get('edges') or not history.get('pageInfo', {}).get('hasNextPage'):
        return addition_total, deletion_total, my_commits
    return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    """Queries all accessible repositories (60 at a time) to total lines of code."""
    if edges is None:
        edges = []
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges { node { ... on Repository {
                    nameWithOwner
                    defaultBranchRef { target { ... on Commit { history { totalCount } } } }
                } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    repos_data = request.json().get('data', {}).get('user', {}).get('repositories', {})
    new_edges = repos_data.get('edges') or []
    edges.extend(new_edges)
    if repos_data.get('pageInfo', {}).get('hasNextPage'):
        return loc_query(owner_affiliation, comment_size, force_cache, repos_data['pageInfo']['endCursor'], edges)
    return cache_builder(edges, comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """Recomputes LOC only for repositories whose commit count changed since last cache."""
    valid_edges = [e for e in edges if e and isinstance(e, dict) and e.get('node') and isinstance(e['node'], dict) and e['node'].get('nameWithOwner')]
    cached = True
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data) - comment_size != len(valid_edges) or force_cache:
        cached = False
        flush_cache(valid_edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(valid_edges)):
        if index >= len(data):
            break
        parts = data[index].split()
        if not parts:
            continue
        repo_hash = parts[0]
        commit_count = parts[1] if len(parts) > 1 else '0'
        edge_node = valid_edges[index]['node']
        if repo_hash == hashlib.sha256(edge_node['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                history_count = None
                default_branch = edge_node.get('defaultBranchRef')
                if default_branch and isinstance(default_branch, dict) and default_branch.get('target'):
                    history = default_branch['target'].get('history')
                    if history and isinstance(history, dict):
                        history_count = history.get('totalCount')
                if history_count is not None and int(commit_count) != history_count:
                    owner, repo_name = edge_node['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(history_count) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except (TypeError, KeyError, IndexError, ValueError):
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        if len(loc) >= 5:
            loc_add += int(loc[3])
            loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """Wipes the cache file (keeping the comment block)."""
    data = []
    if comment_size > 0:
        try:
            with open(filename, 'r') as f:
                data = f.readlines()[:comment_size]
        except FileNotFoundError:
            for _ in range(comment_size):
                data.append('This line is a comment block. Write whatever you want here.\n')
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            if node and isinstance(node, dict) and node.get('node') and isinstance(node['node'], dict) and node['node'].get('nameWithOwner'):
                f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def force_close_file(data, cache_comment):
    """Preserves partial data if the program crashes mid-write."""
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """Counts total stars across owned repositories."""
    total_stars = 0
    for node in data:
        if node and isinstance(node, dict) and node.get('node') and isinstance(node['node'], dict):
            stargazers = node['node'].get('stargazers')
            if stargazers and isinstance(stargazers, dict):
                total_stars += stargazers.get('totalCount', 0)
    return total_stars


def svg_overwrite(filename, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data):
    """Parses the SVG and updates the dynamic elements with the latest data."""
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'commit_data', commit_data, 22)
    justify_format(root, 'star_data', star_data, 14)
    justify_format(root, 'repo_data', repo_data, 6)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'follower_data', follower_data, 10)
    justify_format(root, 'loc_data', loc_data[2], 9)
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1], 7)
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """Updates element text and pads the preceding _dots element to right-justify."""
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_string = {0: '', 1: ' ', 2: '. '}[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """Finds the element by id and replaces its text."""
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    """Totals commits using the cache file created by cache_builder."""
    total_commits = 0
    filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        return 0
    data = data[comment_size:]
    for line in data:
        parts = line.split()
        if len(parts) >= 3:
            total_commits += int(parts[2])
    return total_commits


def user_getter(username):
    """Returns the account ID (and creation time) of the user."""
    query_count('user_getter')
    query = '''
    query($login: String!){ user(login: $login) { id createdAt } }'''
    request = simple_request(user_getter.__name__, query, {'login': username})
    user_info = request.json().get('data', {}).get('user', {})
    return {'id': user_info.get('id')}, user_info.get('createdAt')


def follower_getter(username):
    """Returns the follower count of the user."""
    query_count('follower_getter')
    query = '''
    query($login: String!){ user(login: $login) { followers { totalCount } } }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    user_info = request.json().get('data', {}).get('user', {})
    followers = user_info.get('followers', {})
    return int(followers.get('totalCount', 0))


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)

    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)

    for index in range(len(total_loc) - 1):
        total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1])

    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
          '{:<21}'.format('Total function time:'),
          '{:>11}'.format('%.4f' % (user_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
          ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')
    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items():
        print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
