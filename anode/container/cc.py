#!/usr/bin/env python
"""
TODO:
[ ] server and client name argument is a short cut
[ ] generic server and client delivery loop
[ ] decide on how Channel Type is passed/associated with gen server/client
[ ] Entity might be better as a 'factory' that can make handler instances
per request. This will also facilitate the Entity holding 'business'
objects/resources that each request has access to. This will keep the
actual handlers functional. 
"""
__author__ = 'Adam R. Smith'
__license__ = 'Apache 2.0'

from zope.interface import providedBy

from anode.base import CFG, SERVICE_CFG, messaging, channel, GreenProcessSupervisor
from anode.net import entity
from anode.service.service import add_service_by_name, get_service_by_name

from anode.util.log import log
from anode.util.containers import DotDict

class Container(object):
    """
    The Capability Container. Its purpose is to spawn/monitor processes and services
    that do the bulk of the work in the ION system.
    """
    node = None
    def __init__(self, *args, **kwargs):
        self.proc_sup = GreenProcessSupervisor()

    def start(self, server=True):
        log.debug("In Container.start")
        log.debug("server: %s" % str(server))
        self.proc_sup.start() 
        self.node, self.ioloop = messaging.makeNode() # shortcut hack
        self.proc_sup.spawn('green', self.ioloop.join)

        if server == True:
            # Read the config file and start services defined there
            # TODO likely should be done elsewhere
            serviceNames = self.readConfig()
            log.debug("serviceNames: %s" % str(serviceNames))

            # Iterate over service name list, starting services
            for serviceName in serviceNames:
                log.debug("serviceName: %s" % str(serviceName))
                self.start_service(serviceName)

    def readConfig(self):
        log.debug("In Container.readConfig")
        # Loop through configured services and start them
        services = SERVICE_CFG['apps']
        # Return value.  Will contain list of
        # service names from the config file
        serviceNames = []
        for serviceDef in services:
            log.debug("serviceDef: %s" % str(serviceDef))
            name = serviceDef["name"]

            configParams = {}
            if serviceDef.has_key("config"):
                configParams = serviceDef["config"]

            # Service is described in processapp tuple
            # Field 1 is the module name
            # Field 2 is the class name
            module = serviceDef["processapp"][1]
            cls = serviceDef["processapp"][2]

            serviceInstance = self.forname(module, cls, configParams)

            # Inject dependencies
            serviceInstance.clients = DotDict()
            for dependency in serviceInstance.dependencies:
                dependencyService = get_service_by_name(dependency)
                dependencyInterface = list(providedBy(dependencyService))[0]

                client = entity.RPCClientEntityFromInterface(dependencyInterface)
                self.start_client(dependency, client)
                serviceInstance.clients[dependency] = client

            # Add to global dict
            add_service_by_name(name, serviceInstance)

            serviceNames.append(name)

        return serviceNames

    def forname(self, modpath, classname, configParams={}):
        ''' Returns a class of "classname" from module "modname". '''
        log.debug("In Container.forname")
        log.debug("modpath: %s" % modpath)
        log.debug("classname: %s" % classname)
        log.debug("configParams: %s" % str(configParams))
        firstTime = True
        module = __import__(modpath, fromlist=[classname])
        classobj = getattr(module, classname)
        return classobj(configParams)


    def stop(self):
        log.debug("In Container.stop")
        # TODO: Have a choice of shutdown behaviors for waiting on children, timeouts, etc
        self.proc_sup.shutdown(CFG.cc.timeout.shutdown)

    def start_service(self, name):
        log.debug("In Container.start_service")
        log.debug("name: %s" % name)
        self.start_server(name, entity.RPCEntityFromService(get_service_by_name(name)))

    def start_server(self, name, entity):
        """
        Start a new request/response server using the given entity as the
        handler/service
        """
        log.debug("In Container.start_server")
        log.debug("name: %s" % str(name))
        def generic_server(ch, entity):
            log.debug("In generic_server. Binding name: %s" % str(name))
            ch.bind(('amq.direct', name))
            ch.listen()
            while True:
                log.debug("service: %s blocking waiting for message" % str(name))
                req_chan = ch.accept()
                msg = req_chan.recv()
                log.debug("service %s received message: %s" % (str(name),str(msg)))
                entity.message_received(req_chan, msg)

        ch = self.node.channel(channel.Bidirectional)
        self.proc_sup.spawn('green', generic_server, ch, entity)

    def start_client(self, name, entity):
        log.debug("In Container.start_client")
        log.debug("name: %s" % str(name))
        log.debug("entity: %s" % str(entity))
        ch = self.node.channel(channel.BidirectionalClient)
        ch.connect(('amq.direct', name))
        entity.attach_channel(ch)
        def client_recv(ch, entity):
            while True:
                data = ch.recv()
                entity.message_received(data)
        self.proc_sup.spawn('green', client_recv, ch, entity)

    def start_subscriber(self, name, entity):
        log.debug("In Container.start_subscriber")
        log.debug("name: %s" % str(name))
        log.debug("entity: %s" % str(entity))
        def generic_consumer(ch, entity):
            log.debug("In generic_consumer. Binding name: %s" % str(name))
            ch.bind(('amq.direct', name))
            ch.listen()
            while True:
                msg = ch.recv()
                entity.message_received(msg)
            # could use recv_from semantic 
        ch = self.node.channel(channel.PubSub)
        self.proc_sup.spawn('green', generic_consumer, ch, entity)

    def serve_forever(self):
        log.debug("In Container.serve_forever")
        if not self.proc_sup.running:
            #self.start()
            pass
        self.proc_sup.join_children()
