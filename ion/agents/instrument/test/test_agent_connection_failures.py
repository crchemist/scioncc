#!/usr/bin/env python

"""
@package ion.agents.instrument.test.test_instrument_agent
@file ion/agents.instrument/test_instrument_agent.py
@author Edward Hunter
@brief Test cases for R2 instrument agent.
"""

__author__ = 'Edward Hunter'


# Import pyon first for monkey patching.

# Pyon log and config objects.
from pyon.public import log
from pyon.public import CFG

# Standard imports.
import time
import unittest
import os

# 3rd party imports.
import gevent
from gevent.event import AsyncResult
from nose.plugins.attrib import attr
from mock import patch

# Pyon pubsub and event support.
from pyon.event.event import EventSubscriber, EventPublisher
from pyon.ion.stream import StandaloneStreamSubscriber

# Pyon unittest support.
from pyon.util.int_test import IonIntegrationTestCase

# Pyon Object Serialization
from pyon.core.bootstrap import get_obj_registry
from pyon.core.object import IonObjectDeserializer
from pyon.core.object import IonObjectSerializer

# Pyon exceptions.
from pyon.core.exception import BadRequest, Conflict, Timeout, ResourceError
from pyon.core.exception import IonException

# Agent imports.
from pyon.util.context import LocalContextMixin
from pyon.agent.agent import ResourceAgentClient
from pyon.agent.agent import ResourceAgentState
from pyon.agent.agent import ResourceAgentEvent

# Driver imports.
from ion.agents.instrument.driver_int_test_support import DriverIntegrationTestSupport

# Objects and clients.
from interface.objects import AgentCommand
from interface.services.icontainer_agent import ContainerAgentClient
from interface.services.dm.ipubsub_management_service import PubsubManagementServiceClient
from interface.services.dm.idataset_management_service import DatasetManagementServiceClient

# Alarms.
from pyon.public import IonObject
from interface.objects import StreamAlertType, AggregateStatusType

from ooi.timer import Timer

"""
bin/nosetests -s -v --nologcapture --with-pycc ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures
bin/nosetests -s -v --nologcapture ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures
bin/nosetests -s -v --nologcapture ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures.test_lost_connection
bin/nosetests -s -v --nologcapture ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures.test_autoreconnect
bin/nosetests -s -v --nologcapture ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures.test_connect_failed
bin/nosetests -s -v --nologcapture ion/agents/instrument/test/test_agent_connection_failures.py:TestAgentConnectionFailures.test_get_set_alerts
"""

###############################################################################
# Global constants.
###############################################################################

DEV_ADDR = CFG.device.sbe37.host
DEV_PORT = CFG.device.sbe37.port
DATA_PORT = CFG.device.sbe37.port_agent_data_port
CMD_PORT = CFG.device.sbe37.port_agent_cmd_port
PA_BINARY = CFG.device.sbe37.port_agent_binary
DELIM = CFG.device.sbe37.delim
WORK_DIR = CFG.device.sbe37.workdir
DRV_URI = CFG.device.sbe37.dvr_egg

from ion.agents.instrument.test.agent_test_constants import IA_RESOURCE_ID
from ion.agents.instrument.test.agent_test_constants import IA_NAME
from ion.agents.instrument.test.agent_test_constants import IA_MOD
from ion.agents.instrument.test.agent_test_constants import IA_CLS
from ion.agents.instrument.test.load_test_driver_egg import load_egg
DVR_CONFIG = load_egg()

# Load MI modules from the egg
from mi.core.instrument.instrument_driver import DriverConnectionState
from mi.instrument.seabird.sbe37smb.ooicore.driver import SBE37ProtocolEvent


class FakeProcess(LocalContextMixin):
    """
    A fake process used because the test case is not an ion process.
    """
    name = ''
    id=''
    process_type = ''

#Refactored as stand alone method for starting an instrument agent for use in other tests, like governance
#to do policy testing for resource agents
#shenrie
def start_instrument_agent_process(container, stream_config={}, resource_id=IA_RESOURCE_ID, resource_name=IA_NAME, org_governance_name=None, message_headers=None):
    log.info("foobar")

    # Create agent config.
    agent_config = {
        'driver_config' : DVR_CONFIG,
        'stream_config' : stream_config,
        'agent'         : {'resource_id': resource_id},
        'test_mode' : True,
        'forget_past' : True,
        'enable_persistence' : False
    }

    if org_governance_name is not None:
        agent_config['org_governance_name'] = org_governance_name


    # Start instrument agent.

    log.info("TestInstrumentAgent.setup(): starting IA.")
    container_client = ContainerAgentClient(node=container.node,
        name=container.name)

    log.info("Agent setup")
    ia_pid = container_client.spawn_process(name=resource_name,
        module=IA_MOD,
        cls=IA_CLS,
        config=agent_config, headers=message_headers)

    log.info('Agent pid=%s.', str(ia_pid))

    # Start a resource agent client to talk with the instrument agent.

    ia_client = ResourceAgentClient(resource_id, process=FakeProcess())
    log.info('Got ia client %s.', str(ia_client))

    return ia_client

@attr('HARDWARE', group='sa')
@patch.dict(CFG, {'endpoint':{'receive':{'timeout': 300}}})
@unittest.skipIf((not os.getenv('PYCC_MODE', False)) and os.getenv('CEI_LAUNCH_TEST', False), 'Skip until tests support launch port agent configurations.')
class TestAgentConnectionFailures(IonIntegrationTestCase):
    """
    Test cases for instrument agent class. Functions in this class provide
    instrument agent integration tests and provide a tutorial on use of
    the agent setup and interface.
    """
    
    ############################################################################
    # Setup, teardown.
    ############################################################################
        
    def setUp(self):
        """
        Set up driver integration support.
        Start port agent, add port agent cleanup.
        Start container.
        Start deploy services.
        Define agent config, start agent.
        Start agent client.
        """
        self._ia_client = None

        # Start container.
        log.info('Staring capability container.')
        self._start_container()
        
        # Bring up services in a deploy file (no need to message)
        log.info('Staring deploy services.')
        self.container.start_rel_from_url('res/deploy/r2deploy.yml')

        log.info('Creating driver integration test support:')
        log.info('driver uri: %s', DRV_URI)
        log.info('device address: %s', DEV_ADDR)
        log.info('device port: %s', DEV_PORT)
        log.info('log delimiter: %s', DELIM)
        log.info('work dir: %s', WORK_DIR)
        self._support = DriverIntegrationTestSupport(None,
                                                     None,
                                                     DEV_ADDR,
                                                     DEV_PORT,
                                                     DATA_PORT,
                                                     CMD_PORT,
                                                     PA_BINARY,
                                                     DELIM,
                                                     WORK_DIR)
        
        # Start port agent, add stop to cleanup.
        self._start_pagent()
        self.addCleanup(self._support.stop_pagent)    
        
        log.info('building stream configuration')
        # Setup stream config.
        self._build_stream_config()

        # Start a resource agent client to talk with the instrument agent.
        log.info('starting IA process')
        self._ia_client = start_instrument_agent_process(self.container, self._stream_config)
        self.addCleanup(self._verify_agent_reset)
        log.info('test setup complete')


    ###############################################################################
    # Port agent helpers.
    ###############################################################################
        
    def _start_pagent(self):
        """
        Construct and start the port agent.
        """

        port = self._support.start_pagent()
        log.info('Port agent started at port %i',port)
        
        # Configure driver to use port agent port number.
        DVR_CONFIG['comms_config'] = {
            'addr' : 'localhost',
            'port' : port,
            'cmd_port' : CMD_PORT
        }
                        
    def _verify_agent_reset(self):
        """
        Check agent state and reset if necessary.
        This called if a test fails and reset hasn't occurred.
        """
        if self._ia_client is None:
            return

        state = self._ia_client.get_agent_state(timeout=120.1)
        if state != ResourceAgentState.UNINITIALIZED:
            cmd = AgentCommand(command=ResourceAgentEvent.RESET)
            retval = self._ia_client.execute_agent(cmd,timeout=300)
            
    ###############################################################################
    # Event helpers.
    ###############################################################################

    def _start_event_subscriber(self, type='ResourceAgentEvent', count=0):
        """
        Start a subscriber to the instrument agent events.
        @param type The type of event to catch.
        @count Trigger the async event result when events received reaches this.
        """
        def consume_event(*args, **kwargs):
            log.info('Test recieved ION event: args=%s, kwargs=%s, event=%s.', 
                     str(args), str(kwargs), str(args[0]))
            self._events_received.append(args[0])
            if self._event_count > 0 and \
                self._event_count == len(self._events_received):
                self._async_event_result.set()
            
        # Event array and async event result.
        self._event_count = count
        self._events_received = []
        self._async_event_result = AsyncResult()
            
        self._event_subscriber = EventSubscriber(
            event_type=type, callback=consume_event,
            origin=IA_RESOURCE_ID)
        self._event_subscriber.start()
        self._event_subscriber._ready_event.wait(timeout=5)

    def _stop_event_subscriber(self):
        """
        Stop event subscribers on cleanup.
        """
        self._event_subscriber.stop()
        self._event_subscriber = None

    ###############################################################################
    # Data stream helpers.
    ###############################################################################

    def _build_stream_config(self):
        """
        """
        # Create a pubsub client to create streams.
        pubsub_client = PubsubManagementServiceClient(node=self.container.node)
        dataset_management = DatasetManagementServiceClient()
        
        encoder = IonObjectSerializer()
        
        # Create streams and subscriptions for each stream named in driver.
        self._stream_config = {}

        stream_name = 'parsed'
        param_dict_name = 'ctd_parsed_param_dict'
        pd_id = dataset_management.read_parameter_dictionary_by_name(param_dict_name, id_only=True)
        stream_def_id = pubsub_client.create_stream_definition(name=stream_name, parameter_dictionary_id=pd_id)
        stream_def = pubsub_client.read_stream_definition(stream_def_id)
        stream_def_dict = encoder.serialize(stream_def)        
        pd = stream_def.parameter_dictionary
        stream_id, stream_route = pubsub_client.create_stream(name=stream_name,
                                                exchange_point='science_data',
                                                stream_definition_id=stream_def_id)
        stream_config = dict(routing_key=stream_route.routing_key,
                                 exchange_point=stream_route.exchange_point,
                                 stream_id=stream_id,
                                 parameter_dictionary=pd,
                                 stream_def_dict=stream_def_dict)
        self._stream_config[stream_name] = stream_config

        stream_name = 'raw'
        param_dict_name = 'ctd_raw_param_dict'
        pd_id = dataset_management.read_parameter_dictionary_by_name(param_dict_name, id_only=True)
        stream_def_id = pubsub_client.create_stream_definition(name=stream_name, parameter_dictionary_id=pd_id)
        stream_def = pubsub_client.read_stream_definition(stream_def_id)
        stream_def_dict = encoder.serialize(stream_def)
        pd = stream_def.parameter_dictionary
        stream_id, stream_route = pubsub_client.create_stream(name=stream_name,
                                                exchange_point='science_data',
                                                stream_definition_id=stream_def_id)
        stream_config = dict(routing_key=stream_route.routing_key,
                                 exchange_point=stream_route.exchange_point,
                                 stream_id=stream_id,
                                 parameter_dictionary=pd,
                                 stream_def_dict=stream_def_dict)
        self._stream_config[stream_name] = stream_config

    def _start_data_subscribers(self, count, raw_count):
        """
        """        
        # Create a pubsub client to create streams.
        pubsub_client = PubsubManagementServiceClient(node=self.container.node)
                
        # Create streams and subscriptions for each stream named in driver.
        self._data_subscribers = []
        self._samples_received = []
        self._raw_samples_received = []
        self._async_sample_result = AsyncResult()
        self._async_raw_sample_result = AsyncResult()

        # A callback for processing subscribed-to data.
        def recv_data(message, stream_route, stream_id):
            log.info('Received parsed data on %s (%s,%s)', stream_id, stream_route.exchange_point, stream_route.routing_key)
            self._samples_received.append(message)
            if len(self._samples_received) == count:
                self._async_sample_result.set()

        def recv_raw_data(message, stream_route, stream_id):
            log.info('Received raw data on %s (%s,%s)', stream_id, stream_route.exchange_point, stream_route.routing_key)
            self._raw_samples_received.append(message)
            if len(self._raw_samples_received) == raw_count:
                self._async_raw_sample_result.set()

        from pyon.util.containers import create_unique_identifier

        stream_name = 'parsed'
        parsed_config = self._stream_config[stream_name]
        stream_id = parsed_config['stream_id']
        exchange_name = create_unique_identifier("%s_queue" %
                    stream_name)
        self._purge_queue(exchange_name)
        sub = StandaloneStreamSubscriber(exchange_name, recv_data)
        sub.start()
        self._data_subscribers.append(sub)
        sub_id = pubsub_client.create_subscription(name=exchange_name, stream_ids=[stream_id],timeout=120.2)
        pubsub_client.activate_subscription(sub_id,timeout=120.3)
        sub.subscription_id = sub_id # Bind the subscription to the standalone subscriber (easier cleanup, not good in real practice)
        
        stream_name = 'raw'
        parsed_config = self._stream_config[stream_name]
        stream_id = parsed_config['stream_id']
        exchange_name = create_unique_identifier("%s_queue" %
                    stream_name)
        self._purge_queue(exchange_name)
        sub = StandaloneStreamSubscriber(exchange_name, recv_raw_data)
        sub.start()
        self._data_subscribers.append(sub)
        sub_id = pubsub_client.create_subscription(name=exchange_name, stream_ids=[stream_id],timeout=120.4)
        pubsub_client.activate_subscription(sub_id,timeout=120.5)
        sub.subscription_id = sub_id # Bind the subscription to the standalone subscriber (easier cleanup, not good in real practice)

    def _purge_queue(self, queue):
        xn = self.container.ex_manager.create_xn_queue(queue)
        xn.purge()
 
    def _stop_data_subscribers(self):
        for subscriber in self._data_subscribers:
            pubsub_client = PubsubManagementServiceClient()
            if hasattr(subscriber,'subscription_id'):
                try:
                    pubsub_client.deactivate_subscription(subscriber.subscription_id,timeout=120.6)
                except:
                    pass
                pubsub_client.delete_subscription(subscriber.subscription_id,timeout=120.7)
            subscriber.stop()

    ###############################################################################
    # Socket listen.
    ###############################################################################

    def _socket_listen(self, s, prompt, timeout):

        buf = ''
        starttime = time.time()
        while True:
            try:
                buf += s.recv(1024)
                print '##### Listening, got: %s' % buf
                if prompt and buf.find(prompt) != -1:
                    break
            except:
                gevent.sleep(1)
            
            finally:
                delta = time.time() - starttime
                if delta > timeout:
                    break
        return buf            
                
    ###############################################################################
    # Assert helpers.
    ###############################################################################
        
    def assertSampleDict(self, val):
        """
        Verify the value is a sample dictionary for the sbe37.
        """
        # AgentCommandResult.result['parsed']
        """
        {'quality_flag': 'ok', 'preferred_timestamp': 'driver_timestamp',
        'stream_name': 'parsed', 'pkt_format_id': 'JSON_Data',
        'pkt_version': 1, 'values':
        [{'value_id': 'temp', 'value': 21.4894},
        {'value_id': 'conductivity', 'value': 13.22157},
        {'value_id': 'pressure', 'value': 146.186}],
        'driver_timestamp': 3556901018.170206}
        """
        
        self.assertIsInstance(val, dict)
        self.assertTrue(val.has_key('values'))
        values_list = val['values']
        self.assertTrue(isinstance(values_list, list))
        self.assertTrue(len(values_list)==3)
        
        ids = ['temp', 'conductivity', 'pressure']
        ids_found = []

        for x in values_list:
            self.assertTrue(x.has_key('value_id'))
            self.assertTrue(x.has_key('value'))
            ids_found.append(x['value_id'])
            self.assertTrue(isinstance(x['value'], float))

        self.assertItemsEqual(ids, ids_found)

        self.assertTrue(val.has_key('driver_timestamp'))
        time = val['driver_timestamp']
        self.assertTrue(isinstance(time, float))
        
    def assertParamDict(self, pd, all_params=False):
        """
        Verify all device parameters exist and are correct type.
        """
        if all_params:
            self.assertEqual(set(pd.keys()), set(PARAMS.keys()))
            for (key, type_val) in PARAMS.iteritems():
                if type_val == list or type_val == tuple:
                    self.assertTrue(isinstance(pd[key], (list, tuple)))
                else:
                    self.assertTrue(isinstance(pd[key], type_val))

        else:
            for (key, val) in pd.iteritems():
                self.assertTrue(PARAMS.has_key(key))
                self.assertTrue(isinstance(val, PARAMS[key]))
        
    def assertParamVals(self, params, correct_params):
        """
        Verify parameters take the correct values.
        """
        self.assertEqual(set(params.keys()), set(correct_params.keys()))
        for (key, val) in params.iteritems():
            correct_val = correct_params[key]
            if isinstance(val, float):
                # Verify to 5% of the larger value.
                max_val = max(abs(val), abs(correct_val))
                self.assertAlmostEqual(val, correct_val, delta=max_val*.01)

            elif isinstance(val, (list, tuple)):
                # list of tuple.
                self.assertEqual(list(val), list(correct_val))
            
            else:
                # int, bool, str.
                self.assertEqual(val, correct_val)

    ###############################################################################
    # Tests.
    ###############################################################################
                
    def test_lost_connection(self):
        """
        test_lost_connection
        """
        
        # Set up a subscriber to collect command events.
        self._start_event_subscriber('ResourceAgentConnectionLostErrorEvent', 1)
        self.addCleanup(self._stop_event_subscriber)    
        
        # Start in uninitialized.
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)
    
        # Initialize the agent.
        cmd = AgentCommand(command=ResourceAgentEvent.INITIALIZE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.INACTIVE)

        # Activate.
        cmd = AgentCommand(command=ResourceAgentEvent.GO_ACTIVE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.IDLE)

        # Go into command mode.
        cmd = AgentCommand(command=ResourceAgentEvent.RUN)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.COMMAND)

        # Start streaming.
        cmd = AgentCommand(command=SBE37ProtocolEvent.START_AUTOSAMPLE)
        retval = self._ia_client.execute_resource(cmd)
        
        # Wait for a while, collect some samples.
        gevent.sleep(10)
        
        # Blow the port agent out from under the agent.
        self._support.stop_pagent()
        
        # Loop until we resyncronize to LOST_CONNECTION/DISCONNECTED.
        # Test will timeout if this dosn't occur.
        while True:
            state = self._ia_client.get_agent_state()
            if state == ResourceAgentState.LOST_CONNECTION:
                break
            else:
                gevent.sleep(1)
        
        # Verify the driver has transitioned to disconnected
        while True:
            state = self._ia_client.get_resource_state()
            if state == DriverConnectionState.DISCONNECTED:
                break
            else:
                gevent.sleep(1)

        # Make sure the lost connection error event arrives.
        self._async_event_result.get(timeout=CFG.endpoint.receive.timeout)                        
        self.assertEqual(len(self._events_received), 1)        

        cmd = AgentCommand(command=ResourceAgentEvent.RESET)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)

    #@unittest.skip('Fails on buildbot for some god unknown reason.')
    def test_autoreconnect(self):
        """
        test_autoreconnect
        """
        # Set up a subscriber to collect command events.
        self._start_event_subscriber('ResourceAgentConnectionLostErrorEvent', 1)
        self.addCleanup(self._stop_event_subscriber)    
        
        # Start in uninitialized.
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)
    
        # Initialize the agent.
        cmd = AgentCommand(command=ResourceAgentEvent.INITIALIZE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.INACTIVE)

        # Activate.
        cmd = AgentCommand(command=ResourceAgentEvent.GO_ACTIVE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.IDLE)

        # Go into command mode.
        cmd = AgentCommand(command=ResourceAgentEvent.RUN)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.COMMAND)


        def poll_func(test):
            cmd = AgentCommand(command=SBE37ProtocolEvent.ACQUIRE_SAMPLE)
            while True:
                try:
                    gevent.sleep(.5)
                    test._ia_client.execute_resource(cmd)
                except IonException as ex:
                    # This exception could be ResourceException (broken pipe)
                    # Timeout or Conflict
                    log.info('#### pre shutdown exception: %s, %s', str(type(ex)), str(ex))
                    break
                    
            while True:
                try:
                    gevent.sleep(.5)
                    test._ia_client.execute_resource(cmd)
                    log.info('#### post shutdown got new sample.')
                    break
                except IonException as ex:
                    # This should be conflict.
                    log.info('#### post shutdown exception: %s, %s', str(type(ex)), str(ex))
                
        timeout = gevent.Timeout(600)
        timeout.start()
        try:

            # Start the command greenlet and let poll for a bit.
            gl = gevent.spawn(poll_func, self)        
            gevent.sleep(20)
        
            # Blow the port agent out from under the agent.
            self._support.stop_pagent()

            # Wait for a while, the supervisor is restarting the port agent.
            gevent.sleep(10)
            self._support.start_pagent()
            
            # Wait for the device to connect and start sampling again.
            gl.join()
            gl = None
            timeout.cancel()
            
        except (Exception, gevent.Timeout) as ex:
            if gl:
                gl.kill()
                gl = None
            self.fail(('Could not reconnect to device: %s,  %s',
                      str(type(ex)), str(ex)))

    def test_connect_failed(self):
        """
        test_connect_failed
        """
        # Stop the port agent.
        self._support.stop_pagent()
        
        # Sleep a bit.
        gevent.sleep(3)
        
        # Start in uninitialized.
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)
    
        # Initialize the agent.
        cmd = AgentCommand(command=ResourceAgentEvent.INITIALIZE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.INACTIVE)

        # Activate. This should fail because there is no port agent to connect to.
        cmd = AgentCommand(command=ResourceAgentEvent.GO_ACTIVE)
        with self.assertRaises(ResourceError):
            retval = self._ia_client.execute_agent(cmd)
            
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.INACTIVE)

    def test_get_set_alerts(self):
        """
        test_get_set_alerts
        Test specific of get/set alerts, including using result of get to
        set later.
        """
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)

        cmd = AgentCommand(command=ResourceAgentEvent.INITIALIZE)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.INACTIVE)

        retval = self._ia_client.get_agent(['alerts'])['alerts']
        self.assertItemsEqual(retval, [])

        alert_def1 = {
            'name' : 'temp_warning_interval',
            'stream_name' : 'parsed',
            'description' : 'Temperature is above normal range.',
            'alert_type' : StreamAlertType.WARNING,
            'aggregate_type' : AggregateStatusType.AGGREGATE_DATA,
            'value_id' : 'temp',
            'lower_bound' : None,
            'lower_rel_op' : None,
            'upper_bound' : 10.5,
            'upper_rel_op' : '<',
            'alert_class' : 'IntervalAlert'
        }
        
        alert_def2 = {
            'name' : 'temp_alarm_interval',
            'stream_name' : 'parsed',
            'description' : 'Temperature is way above normal range.',
            'alert_type' : StreamAlertType.WARNING,
            'aggregate_type' : AggregateStatusType.AGGREGATE_DATA,
            'value_id' : 'temp',
            'lower_bound' : None,
            'lower_rel_op' : None,
            'upper_bound' : 15.5,
            'upper_rel_op' : '<',
            'alert_class' : 'IntervalAlert'
        }


        """
        Interval alerts are returned from get like this:
        (value and status fields describe state of the alert)
        {
        'name': 'temp_warning_interval',
        'stream_name': 'parsed',
        'description': 'Temperature is above normal range.',
        'alert_type': 1,
        'aggregate_type': 2,
        'value_id': 'temp',
        'lower_bound': None,
        'lower_rel_op': None,
        'upper_bound': 10.5,
        'upper_rel_op': '<',
        'alert_class': 'IntervalAlert',

        'status': None,
        'value': None
        }
        """
        
        alert_def3 = {
            'name' : 'late_data_warning',
            'stream_name' : 'parsed',
            'description' : 'Expected data has not arrived.',
            'alert_type' : StreamAlertType.WARNING,
            'aggregate_type' : AggregateStatusType.AGGREGATE_COMMS,
            'time_delta' : 180,
            'alert_class' : 'LateDataAlert'
        }

        """
        Late data alerts are returned from get like this:
        (value and status fields describe state of the alert)
        {
        'name': 'late_data_warning',
        'stream_name': 'parsed',
        'description': 'Expected data has not arrived.',
        'alert_type': 1,
        'aggregate_type': 1,
        'value_id': None,
        'time_delta': 180,
        'alert_class': 'LateDataAlert',
        
        'status': None,
        'value': None
        }
        """
        
        """
        [
            {'status': None,
            'alert_type': 1,
            'name': 'temp_warning_interval',
            'upper_bound': 10.5,
            'lower_bound': None,
            'aggregate_type': 2,
            'alert_class': 'IntervalAlert',
            'value': None,
            'value_id': 'temp',
            'lower_rel_op': None,
            'upper_rel_op': '<',
            'description': 'Temperature is above normal range.'},
            {'status': None,
            'alert_type': 1,
            'name': 'temp_alarm_interval',
            'upper_bound': 15.5,
            'lower_bound': None,
            'aggregate_type': 2,
            'alert_class': 'IntervalAlert',
            'value': None,
            'value_id': 'temp',
            'lower_rel_op': None,
            'upper_rel_op': '<',
            'description': 'Temperature is way above normal range.'},
            {'status': None,
             'stream_name': 'parsed',
             'alert_type': 1,
             'name': 'late_data_warning',
             'aggregate_type': 1,
             'alert_class': 'LateDataAlert',
             'value': None,
             'time_delta': 180,
             'description': 'Expected data has not arrived.'}
        ]
        """
        
        orig_alerts = [alert_def1, alert_def2, alert_def3]
        self._ia_client.set_agent({'alerts' : orig_alerts})
    
        retval = self._ia_client.get_agent(['alerts'])['alerts']
        self.assertTrue(len(retval)==3)
        alerts = retval

        self._ia_client.set_agent({'alerts' : ['clear']})        
        retval = self._ia_client.get_agent(['alerts'])['alerts']
        self.assertItemsEqual(retval, [])

        self._ia_client.set_agent({'alerts' : alerts})
        retval = self._ia_client.get_agent(['alerts'])['alerts']
        self.assertTrue(len(retval)==3)
        
        count = 0
        for x in retval:
            x.pop('status')
            x.pop('value')
            for y in orig_alerts:
                if x['name'] == y['name']:
                    count += 1
                    self.assertItemsEqual(x.keys(), y.keys())
        self.assertEquals(count, 3)
        
        cmd = AgentCommand(command=ResourceAgentEvent.RESET)
        retval = self._ia_client.execute_agent(cmd)
        state = self._ia_client.get_agent_state()
        self.assertEqual(state, ResourceAgentState.UNINITIALIZED)
        
