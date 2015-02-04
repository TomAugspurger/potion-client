# Copyright 2014 Novo Nordisk Foundation Center for Biosustainability, DTU.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from functools import partial
import string
from urllib.parse import urlparse
from jsonschema import validate
import requests
from potion_client import utils
from .constants import *
import logging
from potion_client.exceptions import OneOfException
from potion_client import data_types

logger = logging.getLogger(__name__)
logger.level = logging.DEBUG


_string_formatter = string.Formatter()


class AttributeMapper(object):
    def __init__(self, definition):
        self.read_only = definition.get(READ_ONLY, False)
        self.additional_properties = definition.get(ADDITIONAL_PROPERTIES, False)

        self.__doc__ = definition.get(DOC, None)
        self.definition = definition

        self._attributes = {}
        self._parse_definition()

    def _parse_definition(self):
        if PROPERTIES in self.definition:
            for name, prop in self.definition[PROPERTIES].items():
                self._attributes[name] = AttributeMapper(prop)

        elif ITEMS in self.definition:
            self.definition = self.definition[ITEMS]
            self.__class__ = Items
            self._parse_definition()
        elif ONE_OF in self.definition:
            self.definition = self.definition[ONE_OF]
            self.__class__ = OneOf
            self._parse_definition()

    @property
    def required(self):
        return type(None) in self.types

    @property
    def types(self):
        return utils.type_for(self.definition.get(TYPE, "object"))

    def serialize(self, obj, valid=True):
        if obj is None:
            value = self.empty_value
        elif dict in self.types:
            value = {}
            if self.additional_properties:
                iterator = obj.keys()
            else:
                iterator = self._attributes.keys()
            for key in iterator:

                if key.startswith("$"):
                    try:
                        val = data_types.for_key(key).serialize(obj)
                    except NotImplementedError:
                        val = obj.get(key, None)
                else:
                    val = obj.get(key, None)
                    if key in self._attributes:
                        val = self._attributes[key].serialize(val)

                if val is not None:
                    value[key] = val

        else:
            value = self.types[0](obj)
        if valid:
            validate(value, self.definition)
        return value

    def resolve(self, obj, client):
        if obj is None:
            return self.empty_value

        if PROPERTIES in self.definition:
            key = list(self.definition[PROPERTIES].keys())[0]
            obj = data_types.for_key(key).resolve(obj, client)

        return obj

    @property
    def empty_value(self):
        if self.read_only or type(None) in self.types:
            return None
        elif dict in self.types:
            return {}
        else:
            return None


class OneOf(AttributeMapper):
    def __init__(self, definitions):
        self.definition = definitions
        super(OneOf, self).__init__({})

    def _parse_definition(self):
        for index, definition in enumerate(self.definition):
            self._attributes[index] = AttributeMapper(definition)

    def resolve(self, obj, client):
        errors = []
        for attr in self._attributes.values():
            try:
                return attr.resolve(obj, client)
            except Exception as e:
                errors.append(e)

        raise OneOfException(errors)

    def serialize(self, obj, valid=True):
        if obj is None:
            return None

        errors = []

        for attr in self._attributes.values():
            try:
                return attr.serialize(obj, valid)
            except Exception as e:
                errors.append(e)
        if self.required:
            raise OneOfException(errors)

    @property
    def types(self):
        seen = set()
        all_types = [t for attr in self._attributes.values() for t in attr.types]
        return [x for x in all_types if x not in seen and not seen.add(x)]


class Items(AttributeMapper):
    def __init__(self, *args, **kwargs):
        super(Items, self).__init__(*args, **kwargs)

    def serialize(self, iterable, valid=True):
        if iterable is None and self.required:
            return []

        assert isinstance(iterable, list), "Items expects list"
        return[super(Items, self).serialize(element, self.definition) for element in iterable]

    @property
    def empty_value(self):
        if type(None) in self.types:
            return None
        else:
            return []

    def resolve(self, iterable, client):
        if list is None:
            return self.empty_value

        return[super(Items, self).resolve(element, client) for element in iterable]


class DynamicElement(object):

    def __init__(self, link):
        assert isinstance(link, (Link, type(None))), "Invalid link type (%s) for proxy" % type(link)
        self._link = link

    @property
    def return_type(self):
        raise NotImplementedError

    def _resolve(self):
        pass

    @property
    def __doc__(self):
        return self._link.__doc__


class LinkProxy(DynamicElement):

    def __init__(self, link=None, binding=None, attributes=None, **kwargs):
        super(LinkProxy, self).__init__(link)
        self._kwargs = kwargs
        self._binding = binding
        self._attributes = attributes or {}

    def serialize_attribute_value(self, key, value):
        if key in self._attributes:
            return self._attributes[key].serialize(value)
        return value

    def handler(self, res: requests.Response):
        return res.json()

    def bind(self, instance):
        return self.return_type(link=self._link, binding=instance)

    @property
    def return_type(self):
        if self._link.return_type is list:
            return ListLinkProxy
        elif self._link.return_type is type(None):
            return VoidLinkProxy

        return ObjectLinkProxy

    def __get__(self, instance, owner):
        return self.bind(instance or owner)

    def __repr__(self):
        return "Proxy %s '%s' %s" % (self._link.method, self._link.route.path, self._kwargs)

    def _parse_schema(self):
        if PROPERTIES in self._link.schema:
            properties = self._link.schema[PROPERTIES]
            for prop in properties.keys():
                self._attributes[prop] = AttributeMapper(properties[prop])
                setattr(self, prop, self._proxy(prop))

    def _parse_kwarg(self, key, value):
        if key in self._attributes:
            self._kwargs[key] = self._attributes[key].serialize(value)

    def _proxy(self, prop):

        def new_proxy(*args, **kwargs):
            new_kwargs = self._kwargs
            if len(args) > 0:
                assert len(kwargs) == 0, "Setting args and kwargs is not supported"
                if len(args) == 1:
                    val = args[0]
                else:
                    val = args
            else:
                val = kwargs
            attr = self._attributes.get(prop, None)
            if attr is not None:
                val = attr.serialize(val)
            new_kwargs[prop] = val

            proxy = self.return_type(link=self._link, binding=self._binding, attributes=self._attributes, **new_kwargs)
            return proxy

        return new_proxy

    def _resolve(self, *args, **kwargs):
        new_kwargs = self._kwargs
        new_kwargs.update(kwargs)
        return self._link(*args, handler=self.handler, binding=self._binding, **new_kwargs)


class BoundedLinkProxy(LinkProxy):

    def __init__(self, **kwargs):
        super(BoundedLinkProxy, self).__init__(**kwargs)
        self._parse_schema()

    def __call__(self, *args, **kwargs):
        return self._resolve(*args, **kwargs)


class VoidLinkProxy(BoundedLinkProxy):

    def handler(self, res: requests.Response):
        return None


class ListLinkProxy(BoundedLinkProxy):
    def __init__(self, links=None, **kwargs):
        super(ListLinkProxy, self).__init__(**kwargs)
        self._collection = None
        self._total = 0
        self._links = links or {}

    def handler(self, res: requests.Response):
        try:
            self._total = int(res.headers["X-Total-Count"])
            res.links.pop("self", None)
            [self._create_link(name, link) for name, link in res.links.items()]
        except KeyError:
            self._total = len(res.json())

        return res.json()

    def _create_link(self, name, link):
        if not isinstance(link, LinkProxy):
            url = urlparse(link[URL])
            new_kwargs = self._kwargs

            for key, value in utils.params_to_dictionary(url.query).items():
                new_kwargs[key] = self.serialize_attribute_value(key, value)
            link = ListLinkProxy(link=self._link, binding=self._binding, **new_kwargs)
        setattr(self, name, link)
        self._links[name] = link

    def __iter__(self):
        return ListLinkIterator(self)

    @property
    def slice_size(self):
        if self._collection is None:
            self._collection = self._resolve()

        return len(self._collection)

    def __len__(self):
        if self._collection is None:
            self._collection = self._resolve()
        return self._total

    def __getitem__(self, index: int):
        if self._collection is None:
            self._collection = self._resolve()

        if index > self._total:
            per_page = self._kwargs['per_page']
            page = index/per_page
            kwargs = self._kwargs
            kwargs['page'] = page
            link = ListLinkProxy(link=self._link, binding=self._binding, **kwargs)
            return link[index-page*per_page]

        return utils.evaluate_ref(self._collection[index][URI], self._binding.client, self._collection[index])


class ListLinkIterator(object):

    def __init__(self, list_link: ListLinkProxy):
        if hasattr(list_link, 'first'):
            self._slice_link = list_link.first
        else:
            self._slice_link = list_link

        self.pointer = 0
        self.total = len(self._slice_link)

    def __next__(self):
        if self.pointer >= self._slice_link.slice_size:
            if hasattr(self._slice_link, 'next'):
                self._slice_link = self._slice_link.next
                self.pointer = 0
            else:
                raise StopIteration

        ret = self._slice_link[self.pointer]
        self.pointer += 1
        return ret


class ObjectLinkProxy(BoundedLinkProxy):

    def __init__(self, **kwargs):
        binding = kwargs["binding"]
        assert isinstance(binding, (Resource, type(Resource))), "Invalid link type (%s) for object" % type(binding)
        super(ObjectLinkProxy, self).__init__(**kwargs)

    def __call__(self, *args, **kwargs):
        return self._resolve(*args, **kwargs)


class Route(object):
    def __init__(self, path):
        self.default = None
        self.path = path
        self.keys = utils.extract_keys(path)

    @property
    def is_instance(self):
        return len(self.keys) > 0

    def extract_keys(self, resource):
        object_values = dict([(key, getattr(resource, key, None)) for key in self.keys])
        for key, val in object_values.items():
            if val is None:
                object_values[key] = ""
        return object_values


class Link(object):
    def __init__(self, route, method=GET, schema=None, target_schema=None, requests_kwargs=None, docstring=None):
        self.route = route
        self.method = method
        self.schema = schema
        self.target_schema = target_schema
        self.request_kwargs = requests_kwargs
        self.__doc__ = docstring
        self._attributes = {}

    @property
    def return_type(self) -> type:
        if TYPE in self.target_schema:
            return utils.type_for(self.target_schema[TYPE])[0]
        elif REF in self.target_schema:
            if self.target_schema[REF] == "#":
                return object
        else:
            return type(None)

    @property
    def input_type(self) -> type:
        if TYPE in self.schema:
            return utils.type_for(self.schema[TYPE])[0]
        elif REF in self.schema:
            if self.schema[REF] == "#":
                return object
        else:
            return type(None)

    def __call__(self, *args, binding=None, handler=None, **kwargs):
        url = self.generate_url(binding, self.route)
        params = utils.dictionary_to_params(kwargs)
        body = self._process_args(args)
        res = requests.request(self.method, url=url, json=body, params=params, **self.request_kwargs)
        utils.validate_response_status(res)
        return handler(res)

    def generate_url(self, binding, route):
        base_url = binding.client.base_url
        url = "{base}{path}".format(base=base_url, path=route.path)
        if isinstance(binding, Resource):
            url = url.format(**{k: getattr(binding, str(k)) for k in self.route.keys})
        if url.endswith("/"):
            return url[0:-1]
        logger.debug("Generated url: %s" % url)
        return url

    def _process_args(self, args):
        if self.input_type is list:
            ret = []
            for arg in args:
                if isinstance(arg, Resource):
                    ret.append(arg.valid_instance)
                else:
                    ret.append(arg)
            return ret
        elif self.input_type in [dict, object]:
            if len(args) == 1:
                if isinstance(args[0], Resource):
                    return args[0].valid_instance
                else:
                    return args[0]
            elif len(args) == 0:
                return None
            else:
                raise AttributeError
        elif self.input_type in [float, int, str, bool]:
            if len(args) == 1:
                return self.input_type(args[0])
            elif len(args) == 0:
                return None
            else:
                raise AttributeError

        else:
            return None

    def _validate_out(self, out, binding):
        if REF in self.schema and self.schema[REF] == "#":
            self.target_schema = getattr(binding, "_schema")
        return utils.validate_schema(self.target_schema, out)

    def __repr__(self):
        return "[Link %s '%s']" % (self.method, self.route.path)


class Resource(object):
    client = None
    _schema = None
    _instance_links = None
    _self_route = None
    _attributes = None

    def __init__(self, oid=None, instance=None, **kwargs):
        self._create_proxies()
        self._id = oid
        self._instance = instance
        if oid is None:  # make a new object
            self._instance = {}

        for key, value in kwargs.items():
            try:
                setattr(self, key, value)
            except KeyError:
                pass

    def _create_proxies(self):
        for name, link in self._instance_links.items():
            setattr(self, name, LinkProxy(link=link).bind(self))

    @property
    def valid_instance(self):
        instance = {}

        for key in self._attributes.keys():
            attr = self._attributes[key]
            value = self._instance.get(key, attr.empty_value)
            if not attr.read_only and value is not None:
                instance[key] = value

        return instance

    @property
    def instance(self):
        if self._instance is None:
            self._ensure_instance()
        return self._instance

    @property
    def properties(self):
        return self._schema.get(PROPERTIES, {})

    @classmethod
    def _get_property(cls, name: str, self):
        attr = cls._attributes[name]
        raw = self.instance.get(name, None)
        if raw is None:
            raw = attr.empty_value
        self._instance[name] = raw
        return attr.resolve(raw, self.client)

    @classmethod
    def _set_property(cls, name: str, self, value):
        serialized = cls._attributes[name].serialize(value)
        self.instance[name] = serialized

    @classmethod
    def _del_property(cls, name: str, self):
        self.instance.pop(name, None)

    def __getattr__(self, key: str):
        if key.startswith("$"):
            return self.instance[key]
        else:
            getattr(super(Resource, self), key, self)

    def _ensure_instance(self):
        if self._instance is None:
            self._instance = self.self()

    def save(self):
        if self.id is None:
            assert isinstance(self.create, ObjectLinkProxy), "Invalid proxy type %s" % type(self.create)
            self._instance = self.create(self)
        else:
            assert isinstance(self.update, ObjectLinkProxy), "Invalid proxy type %s" % type(self.create)
            self._instance = self.update(self)

    def refresh(self):
        self._instance = self.self()

    @property
    def id(self):
        if self._id is None:
            if self._instance and (URI in self._instance):
                self._id = utils.parse_uri(self._instance[URI])[-1]
        return self._id

    def __dir__(self):
        return super(Resource, self).__dir__() + list(self._schema[PROPERTIES].keys())

    def __str__(self):
        return "<%s %s: %s>" % (self.__class__, getattr(self, "id"), str(self._instance))

    def __eq__(self, other):
        if self.uri and other.uri:
            return self.uri == other.uri
        else:
            super(Resource, self).__eq__(other)

    @classmethod
    def factory(cls, docstring, name, schema, requests_kwargs, client):
        class_name = utils.camelize(name)

        resource = type(class_name, (cls, ), {})
        resource.__doc__ = docstring
        resource._schema = schema
        resource.client = client
        resource._instance_links = {}
        resource._attributes = {}

        routes = {}

        for link_desc in schema[LINKS]:
            if link_desc[HREF] in routes:
                route = routes[link_desc[HREF]]
            else:
                route = Route(link_desc[HREF])
                routes[link_desc[HREF]] = route

            link = Link(route, method=link_desc[METHOD], schema=link_desc.get(SCHEMA, {}),
                        target_schema=link_desc.get(TARGET_SCHEMA, {}), requests_kwargs=requests_kwargs,
                        docstring=link_desc.get(DOC, None))

            if route.is_instance:
                resource._instance_links[link_desc[REL]] = link
            else:
                setattr(resource, link_desc[REL], LinkProxy(link))

        for name, prop in schema[PROPERTIES].items():
            attr = AttributeMapper(prop)
            resource._attributes[name] = attr
            property_name = name
            if name.startswith("$"):
                property_name = name.replace("$", "")
            if attr.read_only:
                setattr(resource, property_name, property(fget=partial(resource._get_property, name), doc=attr.__doc__))
            else:
                setattr(resource, property_name, property(fget=partial(resource._get_property, name),
                                                          fset=partial(resource._set_property, name),
                                                          fdel=partial(resource._del_property, name),
                                                          doc=attr.__doc__))

        return resource