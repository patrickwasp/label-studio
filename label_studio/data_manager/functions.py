import os
import logging
import ujson as json
from operator import itemgetter
from types import SimpleNamespace
from label_studio.utils.misc import DirectionSwitch, timestamp_to_local_datetime
from label_studio.utils.uri_resolver import resolve_task_data_uri
from label_studio.utils.misc import Settings
from collections import OrderedDict
from datetime import datetime

DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'
TASKS = 'tasks:'
logger = logging.getLogger(__name__)
settings = Settings()


class DataManagerException(Exception):
    pass


def create_default_tabs():
    """ Create default state for all tabs as initialization
    """
    return {
        'tabs': [
            {
                'id': 1,
                'title': 'Tab 1',
                'hiddenColumns': None
            }
        ]
    }


def get_all_columns(project):
    """ Make columns info for the frontend data manager
    """
    result = {'columns': []}

    # frontend uses MST data model, so we need two directional referencing parent <-> child
    task_data_children = []
    i = 0

    data_types = OrderedDict()
    # all data types from import data
    if project.derived_all_input_schema:
        data_types.update({key: 'Unknown' for key in project.derived_all_input_schema})
    # data types from config
    data_types.update(project.data_types.items())

    # remove $undefined$ if there is one type at least in labeling config, because it will be resolved automatically
    if len(project.data_types) > 0:
        data_types.pop(settings.UPLOAD_DATA_UNDEFINED_NAME, None)

    for key, data_type in list(data_types.items())[::-1]:  # make data types from labeling config first
        column = {
            'id': key,
            'title': key if key != settings.UPLOAD_DATA_UNDEFINED_NAME else 'data',
            'type': data_type if data_type in ['Image', 'Audio', 'AudioPlus', 'Unknown'] else 'String',
            'target': 'tasks',
            'parent': 'data',
            'show_in_quickview_default': i == 0
        }
        result['columns'].append(column)
        task_data_children.append(column['id'])
        i += 1

    result['columns'] += [
        # --- Tasks ---
        {
            'id': 'id',
            'title': "Task ID",
            'type': "Number",
            'target': 'tasks',
            'show_in_quickview_default': True
        },
        {
            'id': 'completed_at',
            'title': "Completed at",
            'type': "Datetime",
            'target': 'tasks',
            'help': 'Last completion date'
        },
        {
            'id': 'total_completions',
            'title': "Completions",
            'type': "Number",
            'target': 'tasks',
            'help': 'Total completions per task'
        },
        {
            'id': 'has_cancelled_completions',
            'title': "Cancelled",
            'type': "Number",
            'target': 'tasks',
            'help': 'Number of cancelled (skipped) completions'
        },
        {
            'id': 'total_predictions',
            'title': "Predictions",
            'type': "Number",
            'target': 'tasks',
            'help': 'Total predictions per task'
        },
        {
            'id': 'data',
            'title': "data",
            'type': "List",
            'target': 'tasks',
            'children': task_data_children
        }
    ]
    return result


def remove_tabs(project):
    tab_path = os.path.join(project.path, 'tabs.json')
    try:
        os.remove(tab_path)
    except Exception as e:
        logger.error("Can't remove tabs " + str(e) + ": " + tab_path)


def load_all_tabs(project) -> dict:
    """ Load all tabs from disk
    """
    tab_path = os.path.join(project.path, 'tabs.json')
    if os.path.exists(tab_path):
        with open(tab_path, encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = create_default_tabs()
    return data


def save_all_tabs(project, data: dict):
    """ Save all tabs to disk
    """
    tab_path = os.path.join(project.path, 'tabs.json')
    with open(tab_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)


def load_tab(tab_id, project=None, raise_if_not_exists=False):
    """ Load tab info from DB
    """
    data = load_all_tabs(project)

    # select by tab id
    for tab in data['tabs']:
        if tab['id'] == tab_id:
            break
    else:
        if raise_if_not_exists:
            raise DataManagerException('No tab with id: ' + str(tab_id))

        # create a new tab
        tab = {'id': tab_id}
    return tab


def save_tab(tab_id, tab_data, project):
    """ Save tab info to DB
    """
    # load tab data
    data = load_all_tabs(project)
    tab_data['id'] = tab_id

    # select by tab id
    for i, tab in enumerate(data['tabs']):
        if tab['id'] == tab_id:
            data['tabs'][i] = tab_data
            break
    else:
        # create a new tab
        tab_data['id'] = tab_id
        data['tabs'].append(tab_data)

    save_all_tabs(project, data)


def delete_tab(tab_id, project):
    """ Delete tab from DB
    """
    data = load_all_tabs(project)

    # select by tab id
    for i, tab in enumerate(data['tabs']):
        if tab['id'] == tab_id:
            del data['tabs'][i]
            break
    else:
        return False

    save_all_tabs(project, data)
    return True


def get_completed_at(task):
    """ Get completed time for task
    """
    # check for empty array []
    if len(task.get('completions', [])) == 0:
        return None

    # aggregate completion created_at by max
    try:
        return max(task['completions'], key=itemgetter('created_at'))['created_at']
    except Exception as exc:
        return 0


def get_cancelled_number(task):
    """ Get was_cancelled (skipped) status for task: returns cancelled completion number for task
    """
    try:
        # note: skipped will be deprecated
        return sum([completion.get('skipped', False) or completion.get('was_cancelled', False)
                    for completion in task['completions']])
    except Exception as exc:
        return None


def preload_task(project, task_id, resolve_uri=False):
    """ Preload task: get completed_at, has_cancelled_completions,
        evaluate pre-signed urls for storages, aggregate over completion data, etc.
    """
    task = project.get_task_with_completions(task_id)

    # no completions at task, get task without completions
    if task is None:
        task = project.source_storage.get(task_id)

    # with completions
    else:
        # completed_at
        completed_at = get_completed_at(task)
        if completed_at != 0 and isinstance(completed_at, int):
            completed_at = timestamp_to_local_datetime(completed_at).strftime(DATETIME_FORMAT)
        task['completed_at'] = completed_at

        # cancelled completions number
        task['has_cancelled_completions'] = get_cancelled_number(task)

        # total completions and predictions
        task['total_completions'] = len(task['completions'])
        task['total_predictions'] = len(task['predictions'])

    # don't resolve data (s3/gcs is slow) if it's not necessary (it's very slow)
    if resolve_uri:
        task = resolve_task_data_uri(task, project=project)

    # resolve special reserved undefined key
    if project.data_types:
        new_key = next(iter(project.data_types))
        for key, value in task['data'].items():
            if key == settings.UPLOAD_DATA_UNDEFINED_NAME:
                task['data'][new_key] = value
                task['data'].pop(key)

    return task


def preload_tasks(project, resolve_uri=False, max_count=None):
    """ Preload many tasks
    """
    task_ids = project.source_storage.ids()  # get task ids for all tasks in DB

    # get tasks with completions
    tasks = []
    for i in task_ids:
        task = preload_task(project, i, resolve_uri)
        tasks.append(task)
        if max_count is not None and len(tasks) >= max_count:
            break

    return tasks


def task_value_converter(x, data_type):
    """ Convert task value to selected type, because user data could be noisy
    """
    if x is None:
        return None

    if data_type == 'Number' and (not isinstance(x, int) or not isinstance(x, float)):
        return float(x)
    if data_type == 'Datetime' and isinstance(x, str):
        return datetime.strptime(x, DATETIME_FORMAT)

    # list, dict, set, ...
    if not isinstance(x, str):
        return str(x)

    return x


def filter_value_converter(x, data_type):
    """ Convert filter value, from Datetime commonly
    """
    if x is None:
        return None
    if data_type == 'Datetime' and isinstance(x, str):
        return datetime.strptime(x, DATETIME_FORMAT)
    if data_type == 'Datetime' and isinstance(x, dict) and 'min' in x and 'max' in x:
        mini = datetime.strptime(x['min'], DATETIME_FORMAT)
        maxi = datetime.strptime(x['max'], DATETIME_FORMAT)
        return {'min': mini, 'max': maxi}
    return x


def operator(op, a, b, data_type):
    """ Filter operators

        :param op: operation type
        :param a: value from filter
        :param b: value from task
        :param data_type: type of task value
    """
    if op == 'empty':  # TODO: check it
        value = b is None or (hasattr(b, '__len__') and len(b) == 0)
        return value if a else not value

    if a is None:
        return False
    if b is None:
        return False

    b = task_value_converter(b, data_type)

    if op == 'equal':
        return a == b
    if op == 'not_equal':
        return a != b
    if op == 'contains':
        return a in b
    if op == 'not_contains':
        return a not in b

    if op == 'less':
        return b < a
    if op == 'greater':
        return b > a
    if op == 'less_or_equal':
        return b <= a
    if op == 'greater_or_equal':
        return b >= a

    if op == 'in':
        return a['min'] <= b <= a['max']
    if op == 'not_in':
        return not (a['min'] <= b <= a['max'])

    raise DataManagerException('Incorrect operator name in filters: ' + str(op))


def resolve_task_field(task, field):
    """ Get task field from root or 'data' sub-dict
    """
    if field.startswith('data.'):
        result = task['data'].get(field[5:], None)
    else:
        result = task.get(field, None)
    return result


def check_order_enabled(params):
    ordering = params.tab.get('ordering', [])  # ordering = ['id', 'completed_at', ...]
    if ordering is None:
        return False
    return True


def order_tasks(params, tasks):
    """ Apply ordering to tasks
    """
    ordering = params.tab.get('ordering', [])  # ordering = ['id', 'completed_at', ...]
    if not check_order_enabled(params):
        return tasks

    # remove 'tasks:' prefix for tasks api, for annotations it will be 'annotations:'
    ordering = [o.replace(TASKS, '') for o in ordering if o.startswith(TASKS) or o.startswith('-' + TASKS)]
    order = 'id' if not ordering else ordering[0]  # we support only one column ordering right now

    # ascending or descending
    ascending = order[0] == '-'
    order = order[1:] if order[0] == '-' else order

    # id
    if order == 'id':
        ordered = sorted(tasks, key=lambda x: x['id'], reverse=ascending)

    # cancelled: for has_cancelled_completions use two keys ordering
    elif order == 'has_cancelled_completions':
        ordered = sorted(tasks,
                         key=lambda x: (DirectionSwitch(x.get('has_cancelled_completions', None), not ascending),
                                        DirectionSwitch(x.get('completed_at', None), False)))
    # another orderings
    else:
        ordered = sorted(tasks, key=lambda x: (DirectionSwitch(resolve_task_field(x, order), not ascending)))

    return ordered


def check_filters_enabled(params):
    """ Check if filters are enabled
    """
    tab = params.tab
    filters = tab.get('filters', None)
    if tab is None:
        return False
    if not filters or not filters.get('items', None) or not filters.get('conjunction', None):
        return False
    return True
    

def filter_tasks(tasks, params):
    """ Filter tasks using
    """
    # check for filtering params
    tab = params.tab
    filters = tab.get('filters', None)
    if not check_filters_enabled(params):
        return tasks

    conjunction = filters['conjunction']
    new_tasks = tasks if conjunction == 'and' else []

    # go over all the filters
    for f in filters['items']:
        parts = f['filter'].split(':')  # filters:<tasks|annotations>:field_name
        if len(parts) < 3:
            raise DataManagerException('Filter name must be "filters:tasks:<field>" or "filters:tasks:data.<value>"'
                                       'but "' + f['filter'] + '" found')
        target = parts[1]  # 'tasks | annotations'
        field = parts[2]  # field name
        op, value, data_type = f['operator'], f['value'], f['type']
        value = filter_value_converter(value, data_type)

        if target != 'tasks':
            raise DataManagerException('Filtering target ' + target + ' is not yet supported')

        if conjunction == 'and':
            new_tasks = [task for task in new_tasks if operator(op, value, resolve_task_field(task, field), data_type)]

        elif conjunction == 'or':
            new_tasks += [task for task in tasks if operator(op, value, resolve_task_field(task, field), data_type)]

        else:
            raise DataManagerException('Filtering conjunction "' + op + '" is not supported')

    return new_tasks


def prepare_tasks(project, params):
    """ Main function to get tasks
    """
    page, page_size = params.page, params.page_size

    # use max count to speed up evaluation of tasks
    max_count = None if check_filters_enabled(params) or check_order_enabled(params) or page <= 0 or page_size <= 0 \
        else page * page_size

    # load all tasks from db with some aggregations over completions
    tasks = preload_tasks(project, resolve_uri=False, max_count=max_count)

    # filter
    tasks = filter_tasks(tasks, params)

    # order
    tasks = order_tasks(params, tasks)
    total = len(tasks)

    # aggregations
    total_completions, total_predictions = 0, 0
    for task in tasks:
        total_completions += task.get('total_completions', 0)
        total_predictions += task.get('total_predictions', 0)

    # pagination
    if page > 0 and page_size > 0:
        tasks = tasks[(page - 1) * page_size:page * page_size]

    # use only necessary fields to avoid storage (s3/gcs/etc) overloading
    need_uri_resolving = True
    if hasattr(params, 'fields'):  # TODO: or tab.hiddenColumns
        need_uri_resolving = any(['data.' in field for field in params.fields])

    # resolve all task fields
    if need_uri_resolving:
        for i, task in enumerate(tasks):
            tasks[i] = resolve_task_data_uri(task, project=project)

    return {'tasks': tasks,
            'total': total, 'total_completions': total_completions, 'total_predictions': total_predictions}


def prepare_annotations(tasks, params):
    """ Main function to get annotations
        TODO: it's a draft only
    """
    page, page_size = params.page, params.page_size

    # unpack completions from tasks
    items = []
    for task in tasks:
        completions = task.get('completions', [])

        # assign task ids to have link between completion and task in the data manager
        for completion in completions:
            completion['task_id'] = task['id']
            # convert created_at
            created_at = completion.get('created_at', None)
            if created_at:
                completion['created_at'] = timestamp_to_local_datetime(created_at).strftime(DATETIME_FORMAT)

        items += completions

    total = len(items)

    # skip pagination if page<0 and page_size<=0
    if page > 0 and page_size > 0:
        items = items[(page - 1) * page_size: page * page_size]

    return {'annotations': items, 'total': total}


def eval_task_ids(project, filters, ordering):
    """ Apply filter and ordering to all tasks
    """
    tab = {'filters': filters, 'ordering': ordering}
    data = prepare_tasks(project, params=SimpleNamespace(page=-1, page_size=-1, tab=tab, fields=['id']))
    return [t['id'] for t in data['tasks']]


def get_selected_items(project, selected, filters, ordering):
    """ Get selected items

        :param project: LS project
        :param selected: dict {'all': true|false, 'included|excluded': [...task_ids...]}
        :param filters: filters as on tab
        :param ordering: ordering as on tab
    """
    # all_tasks - excluded
    if selected.get('all', False):
        items = eval_task_ids(project, filters=filters, ordering=ordering)  # get tasks from tab filters
        for value in selected.get('excluded', []):
            items.remove(value)
    # included only
    else:
        items = selected.get('included', [])
    return items
