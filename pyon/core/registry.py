#!/usr/bin/env python

__author__ = 'Adam Smith, Tom Lennan'
__license__ = 'Apache 2.0'

import inspect

from pyon.core.exception import NotFound
from pyon.util.log import log

import interface.objects
import interface.messages

model_classes = {}
message_classes = {}

def getextends(type):
    """
    Returns a list of classes that the object with the given type extends.
    @param type (str) Object type
    @retval List of object types that are extended by given type
    """
    ret = []
    base_clzz = model_classes[type]
    for name in model_classes:
        clzz = model_classes[name]
        bases = inspect.getmro(clzz)
        if base_clzz in bases:
            ret.append(name)
    return ret

def get_message_class_parm_type(service_name, service_operation, parameter, in_out):
    """
    Utility function to return the type for the specified parameters
    """

    class_name = service_name + '_' + service_operation + '_' + in_out
    if class_name in message_classes:
        cls = message_classes[class_name]
    else:
        raise NotFound("Message class $%s is not found in the ION registry." % class_name)

    if parameter in cls._schema:
        parm_type = cls._schema[parameter]['type']
    else:
        raise NotFound("Parameter %s not found in class %s" % (parameter, class_name))

    return parm_type

def get_message_class_in_parm_type(service_name, service_operation, parameter):
    """
    Helper function for get_message_class_parm_type
    """
    return  get_message_class_parm_type(service_name, service_operation, parameter, 'in')


def get_message_class_out_parm_type(service_name, service_operation, parameter):
    """
    Helper function for get_message_class_parm_type
    """
    return  get_message_class_parm_type(service_name, service_operation, parameter, 'out')



class IonObjectRegistry(object):
    """
    A simple key-value store that stores by name and by definition hash for versioning.
    Also includes optional persistence to a document database.
    """

    do_validate = True

    def __init__(self):
        classes = inspect.getmembers(interface.objects, inspect.isclass)
        for name, clzz in classes:
            model_classes[name] = clzz
        classes = inspect.getmembers(interface.messages, inspect.isclass)
        for name, clzz in classes:
            message_classes[name] = clzz

    def new(self, _def, _dict=None, **kwargs):
        """ See get_def() for definition lookup options. """
        #log.debug("In IonObjectRegistry.new")
        #log.debug("name: %s" % _def)
        #log.debug("_dict: %s" % str(_dict))
        #log.debug("kwargs: %s" % str(kwargs))
        if _def in model_classes:
            clzz = model_classes[_def]
        elif _def in message_classes:
            clzz = message_classes[_def]
        else:
            raise NotFound("No matching class found for name %s" % _def)
        if _dict:
            # Apply dict values, then override with kwargs
            keywordargs = _dict
            keywordargs.update(kwargs)
            obj = clzz(**keywordargs)
        else:
            obj = clzz(**kwargs)
            
        return obj

