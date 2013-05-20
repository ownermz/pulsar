'''
An implementation of a :class:`TaskBackend` which uses redis as data server.
Requires python-stdnet_

.. _python-stdnet: https://pypi.python.org/pypi/python-stdnet
'''
import logging

from stdnet import odm

from pulsar import async
from pulsar.apps.tasks import backends, states
from pulsar.utils.log import local_method, local_property
from pulsar.apps.pubsub import PubSub
from pulsar.apps.tasks.backends import TaskCallbacks


LOGGER = logging.getLogger('pulsar.tasks')


class TaskData(odm.StdModel):
    id = odm.SymbolField(primary_key=True)
    name = odm.SymbolField()
    status = odm.SymbolField()
    args = odm.PickleObjectField()
    kwargs = odm.PickleObjectField()
    result = odm.PickleObjectField()
    from_task = odm.SymbolField(required=False)
    time_executed = odm.DateTimeField(index=False)
    time_started = odm.DateTimeField(required=False, index=False)
    time_ended = odm.DateTimeField(required=False, index=False)
    expiry = odm.DateTimeField(required=False, index=False)
    meta = odm.JSONField()
    #
    # List where all TaskData objects are queued
    queue = odm.ListField(class_field=True)
    
    class Meta:
        app_label = 'tasks'
        
    def as_task(self):
        params = dict(self.meta or {})
        for field in self._meta.scalarfields:
            params[field.name] = getattr(self, field.attname, None)
        return backends.Task(self.id, **params)
    
    def __unicode__(self):
        return '%s (%s)' % (self.name, self.status)


class TaskBackend(backends.TaskBackend):
    
    @local_method
    def task_manager(self):
        p = PubSub(backend=self.connection_string, name=self.name)
        p.add_client(self)
        p.subscribe('task_done')
        self.local.pubsub = p
        self.local.models = odm.Router(self.connection_string)
        self.local.models.register(TaskData)
        return self.local.models.taskdata
    
    @local_property
    def watched_tasks(self):
        '''Dictionary of task-id, :class:`pulsar.Deferred` populate
by the ::class:`TaskBackend.save_task` method.'''
        return TaskCallbacks()
    
    def num_tasks(self):
        '''Retrieve the number of tasks in the task queue.'''
        task_manager = self.task_manager()
        return task_manager.queue.size()
    
    def put_task(self, task_id):
        task_manager = self.task_manager()
        task_data = yield self._get_task(task_id)
        if task_data:
            task_data.status = states.QUEUED
            task_data = yield task_data.save()
            yield task_manager.queue.push_back(task_data.id)
            yield task_data.id
    
    def get_task(self, task_id=None, when_done=False, timeout=1):
        task_manager = self.task_manager()
        #
        pool = task_manager.backend.client.connection_pool
        if not task_id:
            #LOGGER.info('CONNECTIONS: AVAILABLE %s, CONCURRENT %s, TOTAL %s',
            #            pool.available_connections, pool.concurrent_connections,
            #            pool.available_connections+pool.concurrent_connections)
            task_id = yield task_manager.queue.block_pop_front(timeout=timeout)
        if task_id:
            task_data = yield self._get_task(task_id)
            if task_data:
                task = task_data.as_task()
                if when_done:
                    yield self.watched_tasks.when_done(task)
                else:
                    yield task
        
    def get_tasks(self, **filters):
        task_manager = self.task_manager()
        tasks = yield task_manager.filter(**filters).all()
        yield [t.as_task() for t in tasks]
        
    def save_task(self, task_id, **params):
        # Called by self when the task need to be saved
        task_manager = self.task_manager()
        task_data = yield self._get_task(task_id)
        if task_data:
            for field, value in params.items():
                if field in task_data._meta.dfields:
                    setattr(task_data, field, value)
                else:
                    # not a field put value in the meta json field
                    task_data.meta[field] = value
            yield task_data.save()
        else:
            task_data = yield task_manager.new(id=task_id, **params)
        task = task_data.as_task()
        self.watched_tasks.when_done(task)
        if task.done():
            # task is done, publish task_id into the task_done channel
            self.local.pubsub.publish('task_done', task_id)
        yield task_id
        
    def delete_tasks(self, ids=None):
        deleted = 0
        if ids:
            task_manager = self.task_manager()
            watched_tasks = self.watched_tasks
            tasks = yield task_manager.filter(id=ids).all()
            yield task_manager.filter(id=ids).delete()
            for task_data in tasks:
                watched_tasks.finish(task_data.as_task())
                deleted += 1
        yield deleted
            
    @async()
    def write(self, task_id):
        '''Got a new message from redis pubsub task_done channel.
This is the write method required by all pubsub clients.'''
        task_data = yield self._get_task(task_id)
        if task_data:
            self.watched_tasks.finish(task_data.as_task())
        
    ############################################################################
    ##    INTERNALS
    def _get_task(self, task_id):
        tasks = yield self.task_manager().filter(id=task_id).all()
        if tasks:
            yield tasks[0]
            