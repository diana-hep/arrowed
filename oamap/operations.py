#!/usr/bin/env python

# Copyright (c) 2017, DIANA-HEP
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import ast
import math
import numbers
import sys
import types
import time

import numpy

import oamap.schema
import oamap.generator
import oamap.proxy
import oamap.util
import oamap.compiler

recastings = {}
transformations = {}
actions = {}

################################################################ general utilities

def _setindexes(input, output):
    if isinstance(input, oamap.proxy.ListProxy):
        if isinstance(output, oamap.proxy.ListProxy):
            output._whence, output._stride, output._length = input._whence, input._stride, input._length
        elif isinstance(output, (oamap.proxy.RecordProxy, oamap.proxy.TupleProxy)):
            output._index = input._whence

    elif isinstance(input, (oamap.proxy.RecordProxy, oamap.proxy.TupleProxy)):
        if isinstance(output, (oamap.proxy.RecordProxy, oamap.proxy.TupleProxy)):
            output._index = input._index
        elif isinstance(output, oamap.proxy.ListProxy):
            output._length = output._length - input._index
            output._whence = input._index

    else:
        raise AssertionError(type(input))

    return output
    
class _DualSource(object):
    def __init__(self, old, oldns):
        self.old = old
        self.new = {}

        i = 0
        self.namespace = None
        while self.namespace is None or self.namespace in oldns:
            self.namespace = "namespace-" + str(i)
            i += 1

        self._arraynum = 0

    def arrayname(self):
        trial = None
        while trial is None or trial in self.new:
            trial = "array-" + str(self._arraynum)
            self._arraynum += 1
        return trial

    def getall(self, roles):
        out = {}

        if hasattr(self.old, "getall"):
            out.update(self.old.getall([x for x in roles if x.namespace != self.namespace]))
        else:
            for x in roles:
                if x.namespace != self.namespace:
                    out[x] = self.old[str(x)]

        if hasattr(self.new, "getall"):
            out.update(self.new.getall([x for x in roles if x.namespace == self.namespace]))
        else:
            for x in roles:
                if x.namespace == self.namespace:
                    out[x] = self.new[str(x)]

        return out

    def put(self, schemanode, *arrays):
        if isinstance(schemanode, oamap.schema.Primitive):
            datarole = oamap.generator.DataRole(self.arrayname(), self.namespace)
            roles2arrays = {datarole: arrays[0]}
            schemanode.data = str(datarole)

        elif isinstance(schemanode, oamap.schema.List):
            startsrole = oamap.generator.StartsRole(self.arrayname(), self.namespace, None)
            stopsrole = oamap.generator.StopsRole(self.arrayname(), self.namespace, None)
            startsrole.stops = stopsrole
            stopsrole.starts = startsrole
            roles2arrays = {startsrole: arrays[0], stopsrole: arrays[1]}
            schemanode.starts = str(startsrole)
            schemanode.stops = str(stopsrole)

        elif isinstance(schemanode, oamap.schema.Union):
            tagsrole = oamap.generator.TagsRole(self.arrayname(), self.namespace, None)
            offsetsrole = oamap.generator.OffsetsRole(self.arrayname(), self.namespace, None)
            tagsrole.offsets = offsetsrole
            offsetsrole.tags = tagsrole
            roles2arrays = {tagsrole: arrays[0], offsetsrole: arrays[1]}
            schemanode.tags = str(tagsrole)
            schemanode.offsets = str(offsetsrole)

        elif isinstance(schemanode, oamap.schema.Record):
            pass

        elif isinstance(schemanode, oamap.schema.Tuple):
            pass

        elif isinstance(schemanode, oamap.schema.Pointer):
            positionsrole = oamap.generator.PositionsRole(self.arrayname(), self.namespace)
            roles2arrays = {positionsrole: arrays[0]}
            schemanode.positions = str(positionsrole)

        else:
            raise AssertionError(schemanode)

        if schemanode.nullable:
            maskrole = oamap.generator.MaskRole(self.arrayname(), self.namespace, dict(roles2arrays))
            roles2arrays[maskrole] = arrays[-1]
            schemanode.mask = str(maskrole)

        schemanode.namespace = self.namespace
        self.putall(roles2arrays)

    def putall(self, roles2arrays):
        if hasattr(self.new, "putall"):
            self.new.putall(roles2arrays)
        else:
            for n, x in roles2arrays.items():
                self.new[str(n)] = x

    def close(self):
        if hasattr(self.old, "close"):
            self.old.close()
        if hasattr(self.new, "close"):
            self.new.close()

    @staticmethod
    def collect(schema, arrays, namespace, prefix, delimiter):
        newarrays = {}
        def getarrays(node):
            if isinstance(node, _DualSource):
                getarrays(node.old)
                for n, x in node.new.items():
                    newarrays[node.namespace, n] = x
        getarrays(arrays)

        oldnames = {}
        def recurse(schema, memo):
            if isinstance(schema, oamap.schema.Primitive):
                if (schema.namespace, schema.data) in newarrays:
                    oldnames[id(schema)] = (schema.namespace, schema.data, schema.mask)
                    schema.namespace = namespace
                    schema.data = None
                    if schema.nullable:
                        schema.mask = None
            elif isinstance(schema, oamap.schema.List):
                if (schema.namespace, schema.starts) in newarrays:
                    oldnames[id(schema)] = (schema.namespace, schema.starts, schema.stops, schema.mask)
                    schema.namespace = namespace
                    schema.starts = None
                    schema.stops = None
                    if schema.nullable:
                        schema.mask = None
                recurse(schema.content, memo)
            elif isinstance(schema, oamap.schema.Union):
                if (schema.namespace, schema.tags) in newarrays:
                    oldnames[id(schema)] = (schema.namespace, schema.tags, schema.offsets, schema.mask)
                    schema.namespace = namespace
                    schema.tags = None
                    schema.offsets = None
                for possibility in schema.possibilities:
                    recurse(possibility, memo)
            elif isinstance(schema, (oamap.schema.Record, oamap.schema.Tuple)):
                if schema.nullable and (schema.namespace, schema.mask) in newarrays:
                    oldnames[id(schema)] = (schema.namespace, schema.mask)
                    schema.namespace = namespace
                    schema.mask = None
                for field in schema.fields.values():
                    recurse(field, memo)
            elif isinstance(schema, oamap.schema.Pointer):
                if id(schema) not in memo:
                    memo.add(id(schema))
                    if (schema.namespace, schema.positions) in newarrays:
                        oldnames[id(schema)] = (schema.namespace, schema.positions, schema.mask)
                        schema.namespace = namespace
                        schema.positions = None
                        if schema.nullable:
                            schema.mask = None
                    recurse(schema.target, memo)
            else:
                raise AssertionError(schema)

        recurse(schema, set())

        generator = schema.generator(prefix=prefix, delimiter=delimiter)

        roles2arrays = {}
        def recurse2(schema, generator, memo):
            if isinstance(generator, oamap.generator.ExtendedGenerator):
                generator = generator.generic

            r2a = {}
            if isinstance(schema, oamap.schema.Primitive):
                if id(schema) in oldnames:
                    oldns, olddata, oldmask = oldnames[id(schema)]
                    datarole = oamap.generator.DataRole(generator.data, generator.namespace)
                    r2a[datarole] = newarrays[oldns, olddata]
                    if schema.nullable:
                        maskrole = oamap.generator.MaskRole(generator.mask, generator.namespace, dict(r2a))
                        r2a[maskrole] = newarrays[oldns, oldmask]

            elif isinstance(schema, oamap.schema.List):
                if id(schema) in oldnames:
                    oldns, oldstarts, oldstops, oldmask = oldnames[id(schema)]
                    startsrole = oamap.generator.StartsRole(generator.starts, generator.namespace, None)
                    stopsrole = oamap.generator.StopsRole(generator.stops, generator.namespace, None)
                    startsrole.stops = stopsrole
                    stopsrole.starts = startsrole
                    r2a[startsrole] = newarrays[oldns, oldstarts]
                    r2a[stopsrole] = newarrays[oldns, oldstops]
                    if schema.nullable:
                        maskrole = oamap.generator.MaskRole(generator.mask, generator.namespace, dict(r2a))
                        r2a[maskrole] = newarrays[oldns, oldmask]
                recurse2(schema.content, generator.content, memo)

            elif isinstance(schema, oamap.schema.Union):
                if id(schema) in oldnames:
                    oldns, oldtags, oldoffsets, oldmask = oldnames[id(schema)]
                    tagsrole = oamap.generator.TagsRole(generator.tags, generator.namespace, None)
                    offsetsrole = oamap.generator.OffsetsRole(generator.offsets, generator.namespace, None)
                    tagsrole.offsets = offsetsrole
                    offsetsrole.tags = tagsrole
                    r2a[tagsrole] = newarrays[oldns, oldtags]
                    r2a[offsetsrole] = newarrays[oldns, oldoffsets]
                    if schema.nullable:
                        maskrole = oamap.generator.MaskRole(generator.mask, generator.namespace, dict(r2a))
                        r2a[maskrole] = newarrays[oldns, oldmask]
                for pschema, pgenerator in zip(schema.possibilities, generator.possibilities):
                    recurse2(pschema, pgenerator, memo)

            elif isinstance(schema, (oamap.schema.Record, oamap.schema.Tuple)):
                if schema.nullable and id(schema) in oldnames:
                    oldns, oldmask = oldnames[id(schema)]
                    maskrole = oamap.generator.MaskRole(generator.mask, generator.namespace, {})
                    r2a[maskrole] = newarrays[oldns, oldmask]
                for n in schema.fields:
                    recurse2(schema[n], generator.fields[n], memo)

            elif isinstance(schema, oamap.schema.Pointer):
                if id(schema) not in memo:
                    memo.add(id(schema))
                    if id(schema) in oldnames:
                        oldns, oldpositions, oldmask = oldnames[id(schema)]
                        positionsrole = oamap.generator.PositionsRole(generator.positions, generator.namespace)
                        r2a[positionsrole] = newarrays[oldns, oldpositions]
                        if schema.nullable:
                            maskrole = oamap.generator.MaskRole(generator.mask, generator.namespace, dict(r2a))
                            r2a[maskrole] = newarrays[oldns, oldmask]
                    recurse2(schema.target, generator.target, memo)

            else:
                raise AssertionError(schema)

            roles2arrays.update(r2a)

        recurse2(schema, generator, set())

        return generator.namedschema(), roles2arrays

################################################################ fieldname/recordname

def fieldname(data, newname, at):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()
        nodes = schema.path(at, parents=True)
        if len(nodes) < 2:
            raise TypeError("path {0} did not match a field in a record".format(repr(at)))

        for n, x in nodes[1].fields.items():
            if x is nodes[0]:
                oldname = n
                break

        del nodes[1][oldname]
        nodes[1][newname] = nodes[0]
        return _setindexes(data, schema(data._arrays))
        
    else:
        raise TypeError("fieldname can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["fieldname"] = fieldname

def recordname(data, newname, at=""):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()
        nodes = schema.path(at, parents=True)
        while isinstance(nodes[0], oamap.schema.List):
            nodes = (nodes[0].content,) + nodes
        if not isinstance(nodes[0], oamap.schema.Record):
            raise TypeError("path {0} did not match a record".format(repr(at)))

        nodes[0].name = newname
        return _setindexes(data, schema(data._arrays))
        
    else:
        raise TypeError("fieldname can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["recordname"] = recordname

################################################################ project/keep/drop

def project(data, at):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema().project(at)
        if schema is None:
            raise TypeError("projection resulted in no schema")
        return _setindexes(data, schema(data._arrays))
    else:
        raise TypeError("project can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["project"] = project

def keep(data, *paths):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema().keep(*paths)
        if schema is None:
            raise TypeError("keep operation resulted in no schema")
        return _setindexes(data, schema(data._arrays))
    else:
        raise TypeError("keep can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["keep"] = keep

def drop(data, *paths):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema().drop(*paths)
        if schema is None:
            raise TypeError("drop operation resulted in no schema")
        return _setindexes(data, schema(data._arrays))
    else:
        raise TypeError("drop can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["drop"] = drop

################################################################ split

def split(data, *paths):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()

        found = False
        for path in paths:
            for nodes in schema.paths(path, parents=True):
                found = True

                if len(nodes) < 4 or not isinstance(nodes[1], oamap.schema.Record) or not isinstance(nodes[2], oamap.schema.List) or not isinstance(nodes[3], oamap.schema.Record):
                    raise TypeError("path {0} matches a field that is not in a Record(List(Record({{field: ...}})))".format(repr(path)))

                datanode, innernode, listnode, outernode = nodes[0], nodes[1], nodes[2], nodes[3]
                for n, x in innernode.fields.items():
                    if x is datanode:
                        innername = n
                        break
                for n, x in outernode.fields.items():
                    if x is listnode:
                        outername = n
                        break

                del innernode[innername]
                if len(innernode.fields) == 0:
                    del outernode[outername]

                outernode[innername] = listnode.copy(content=datanode)

        if not found:
            raise TypeError("none of the paths matched a field")

        return schema(data._arrays)

    else:
        raise TypeError("split can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["split"] = split

################################################################ merge

def merge(data, container, *paths):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()

        constructed = False
        try:
            nodes = schema.path(container, parents=True)

        except ValueError:
            try:
                slash = container.rindex("/")
            except ValueError:
                nodes = (schema,)
                tomake = container
            else:
                tofind, tomake = container[:slash], container[slash + 1:]
                nodes = schema.path(tofind, parents=True)
                container = tofind

            while isinstance(nodes[0], oamap.schema.List):
                nodes = (nodes[0].content,) + nodes
            if not isinstance(nodes[0], oamap.schema.Record):
                raise TypeError("container parent {0} is not a record".format(repr(container)))
            nodes[0][tomake] = oamap.schema.List(oamap.schema.Record({}))
            nodes = (nodes[0][tomake].content, nodes[0][tomake]) + nodes
            constructed = True

        else:
            while isinstance(nodes[0], oamap.schema.List):
                nodes = (nodes[0].content,) + nodes
            
        if len(nodes) < 2 or not isinstance(nodes[0], oamap.schema.Record) or not isinstance(nodes[1], oamap.schema.List):
            raise TypeError("container must be a List(Record(...))")
        
        containerrecord, containerlist = nodes[0], nodes[1]
        parents = nodes[2:]
        listnodes = []
        if not constructed:
            listnodes.append(containerlist)

        for path in paths:
            for nodes in schema.paths(path, parents=True):
                if len(nodes) < 2 or not isinstance(nodes[0], oamap.schema.List) or nodes[1:] != parents:
                    raise TypeError("".format(repr(path)))

                listnode, outernode = nodes[0], nodes[1]
                listnodes.append(listnode)
                
                for n, x in outernode.fields.items():
                    if x is listnode:
                        outername = n
                        break

                del outernode[outername]
                containerrecord[outername] = listnode.content

        if len(listnodes) == 0:
            raise TypeError("at least one path must match schema elements")

        if not all(x.namespace == listnodes[0].namespace and x.starts == listnodes[0].starts and x.stops == listnodes[0].stops for x in listnodes[1:]):
            ### RECONSIDER: without this fallback, merge is a pure recasting (like split) and is easy to decide whether to parallelize
            #
            # starts1, stops1 = data._generator.findbynames("List", listnodes[0].namespace, starts=listnodes[0].starts, stops=listnodes[0].stops)._getstartsstops(data._arrays, data._cache)
            # for x in listnodes[1:]:
            #     starts2, stops2 = data._generator.findbynames("List", x.namespace, starts=x.starts, stops=x.stops)._getstartsstops(data._arrays, data._cache)
            #     if not (starts1 is starts2 or numpy.array_equal(starts1, starts2)) and not (stops1 is stops2 or numpy.array_equal(stops1, stops2)):
            #         raise ValueError("some of the paths refer to lists of different lengths")
            raise ValueError("some of the paths refer to lists of different names")

        if constructed:
            containerlist.namespace = listnodes[0].namespace
            containerlist.starts = listnodes[0].starts
            containerlist.stops = listnodes[0].stops

        return schema(data._arrays)

    else:
        raise TypeError("merge can only be applied to an OAMap proxy (List, Record, Tuple)")

recastings["merge"] = merge

################################################################ parent

def parent(data, fieldname, at):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()
        nodes = schema.path(at, parents=True)
        if len(nodes) < 2:
            raise TypeError("parent operation must be applied to a field of a record")

        parentnode = nodes[1]
        listnode = nodes[0]
        if not isinstance(listnode, oamap.schema.List):
            raise TypeError("parent operation must be applied to a field with list type")
        childnode = listnode.content

        if not isinstance(childnode, oamap.schema.Record):
            raise TypeError("parent operation must be applied to a field with list of records type")

        if listnode.nullable or childnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)
        starts, stops = listgenerator._getstartsstops(data._arrays, data._cache)

        if isinstance(parent.fill, types.FunctionType):
            try:
                import numba as nb
            except ImportError:
                pass
            else:
                parent.fill = nb.jit(nopython=True, nogil=True)(parent.fill)

        pointers = numpy.empty(stops.max() - starts.min(), dtype=oamap.generator.PointerGenerator.posdtype)
        parent.fill(starts, stops, pointers)

        childnode[fieldname] = oamap.schema.Pointer(parentnode)

        arrays = _DualSource(data._arrays, data._generator.namespaces())
        arrays.put(childnode[fieldname], pointers)

        return _setindexes(data, schema(arrays))
            
def _parent_fill(starts, stops, pointers):
    for i in range(len(starts)):
        pointers[starts[i]:stops[i]] = i

parent.fill = _parent_fill
del _parent_fill

transformations["parent"] = parent

################################################################ index

def index(data, fieldname, at):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()
        listnode = schema.path(at)
        if not isinstance(listnode, oamap.schema.List):
            raise TypeError("index operation must be applied to a field with list type")
        childnode = listnode.content

        if not isinstance(childnode, oamap.schema.Record):
            raise TypeError("index operation must be applied to a field with list of records type")

        if listnode.nullable or childnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)
        starts, stops = listgenerator._getstartsstops(data._arrays, data._cache)

        if isinstance(index.fill, types.FunctionType):
            try:
                import numba as nb
            except ImportError:
                pass
            else:
                index.fill = nb.jit(nopython=True, nogil=True)(index.fill)

        values = numpy.empty(stops.max() - starts.min(), dtype=numpy.int32)   # int32
        index.fill(starts, stops, values)

        childnode[fieldname] = oamap.schema.Primitive(values.dtype)

        arrays = _DualSource(data._arrays, data._generator.namespaces())
        arrays.put(childnode[fieldname], values)

        return _setindexes(data, schema(arrays))
            
def _index_fill(starts, stops, pointers):
    for i in range(len(starts)):
        start = starts[i]
        stop = stops[i]
        pointers[start:stop] = numpy.arange(stop - start)

index.fill = _index_fill
del _index_fill

transformations["index"] = index

################################################################ tomask

def tomask(data, at, low, high=None):
    if isinstance(data, oamap.proxy.Proxy):
        schema = data._generator.namedschema()
        nodes = schema.path(at, parents=True)
        while isinstance(nodes[0], oamap.schema.List):
            nodes = (nodes[0].content,) + nodes
        node = nodes[0]

        arrays = _DualSource(data._arrays, data._generator.namespaces())

        if isinstance(node, oamap.schema.Primitive):
            generator = data._generator.findbynames("Primitive", node.namespace, data=node.data, mask=node.mask)

            primitive = generator._getdata(data._arrays, data._cache).copy()
            if node.nullable:
                mask = generator._getmask(data._arrays, data._cache).copy()
            else:
                node.nullable = True
                mask = numpy.arange(len(primitive), dtype=oamap.generator.Masked.maskdtype)

            if high is None:
                if math.isnan(low):
                    selection = numpy.isnan(primitive)
                else:
                    selection = (primitive == low)
            else:
                if math.isnan(low) or math.isnan(high):
                    raise ValueError("if a range is specified, neither of the endpoints can be NaN")
                selection = (primitive >= low)
                numpy.bitwise_and(selection, (primitive <= high), selection)

            mask[selection] = oamap.generator.Masked.maskedvalue

            arrays.put(node, primitive, mask)

        else:
            raise NotImplementedError("tomask operation only defined on primitive fields; {0} matches:\n\n    {1}".format(repr(at), node.__repr__(indent="    ")))

        return _setindexes(data, schema(arrays))

    else:
        raise TypeError("tomask can only be applied to an OAMap proxy (List, Record, Tuple)")

transformations["tomask"] = tomask

################################################################ flatten

def flatten(data, at=""):
    if (isinstance(data, oamap.proxy.ListProxy) and data._whence == 0 and data._stride == 1) or (isinstance(data, oamap.proxy.Proxy) and data._index == 0):
        schema = data._generator.namedschema()
        outernode = schema.path(at)
        if not isinstance(outernode, oamap.schema.List) or not isinstance(outernode.content, oamap.schema.List):
            raise TypeError("path {0} does not refer to a list within a list:\n\n    {1}".format(repr(at), outernode.__repr__(indent="    ")))
        innernode = outernode.content
        if outernode.nullable or innernode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        outergenerator = data._generator.findbynames("List", outernode.namespace, starts=outernode.starts, stops=outernode.stops)
        outerstarts, outerstops = outergenerator._getstartsstops(data._arrays, data._cache)
        innergenerator = data._generator.findbynames("List", innernode.namespace, starts=innernode.starts, stops=innernode.stops)
        innerstarts, innerstops = innergenerator._getstartsstops(data._arrays, data._cache)

        if not numpy.array_equal(innerstarts[1:], innerstops[:-1]):
            raise NotImplementedError("inner arrays are not contiguous: flatten would require the creation of pointers")

        starts = innerstarts[outerstarts]
        stops  = innerstops[outerstops - 1]

        outernode.content = innernode.content

        arrays = _DualSource(data._arrays, data._generator.namespaces())
        arrays.put(outernode, starts, stops)
        return schema(arrays)

    else:
        raise TypeError("flatten can only be applied to a top-level OAMap proxy (List, Record, Tuple)")

transformations["flatten"] = flatten

################################################################ filter

def filter(data, fcn, args=(), at="", numba=True):
    if not isinstance(args, tuple):
        try:
            args = tuple(args)
        except TypeError:
            args = (args,)

    if (isinstance(data, oamap.proxy.ListProxy) and data._whence == 0 and data._stride == 1) or (isinstance(data, oamap.proxy.Proxy) and data._index == 0):
        schema = data._generator.namedschema()
        listnode = schema.path(at)
        if not isinstance(listnode, oamap.schema.List):
            raise TypeError("path {0} does not refer to a list:\n\n    {1}".format(repr(at), listnode.__repr__(indent="    ")))
        if listnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)
        viewstarts, viewstops = listgenerator._getstartsstops(data._arrays, data._cache)
        viewschema = listgenerator.namedschema()
        viewarrays = _DualSource(data._arrays, data._generator.namespaces())
        viewoffsets = numpy.array([viewstarts.min(), viewstops.max()], dtype=oamap.generator.ListGenerator.posdtype)
        viewarrays.put(viewschema, viewoffsets[:1], viewoffsets[-1:])
        view = viewschema(viewarrays)

        params = fcn.__code__.co_varnames[:fcn.__code__.co_argcount]
        avoid = set(params)
        fcnname = oamap.util.varname(avoid, "fcn")
        fillname = oamap.util.varname(avoid, "fill")
        lenname = oamap.util.varname(avoid, "len")
        rangename = oamap.util.varname(avoid, "range")

        ptypes = oamap.util.paramtypes(args)
        if ptypes is not None:
            import numba as nb
            from oamap.compiler import typeof_generator
            ptypes = (typeof_generator(view._generator.content),) + ptypes
        fcn = oamap.util.trycompile(fcn, paramtypes=ptypes, numba=numba)
        rtype = oamap.util.returntype(fcn, ptypes)
        if rtype is not None:
            if rtype != nb.types.boolean:
                raise TypeError("filter function must return boolean, not {0}".format(rtype))

        env = {fcnname: fcn, lenname: len, rangename: range if sys.version_info[0] > 2 else xrange}
        exec("""
def {fill}({view}, {viewstarts}, {viewstops}, {stops}, {pointers}{params}):
    {numitems} = 0
    for {i} in {range}({len}({viewstarts})):
        for {j} in {range}({viewstarts}[{i}], {viewstops}[{i}]):
            {datum} = {view}[{j}]
            if {fcn}({datum}{params}):
                {pointers}[{numitems}] = {j}
                {numitems} += 1
        {stops}[{i}] = {numitems}
    return {numitems}
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           viewstarts=oamap.util.varname(avoid, "viewstarts"),
           viewstops=oamap.util.varname(avoid, "viewstops"),
           stops=oamap.util.varname(avoid, "stops"),
           pointers=oamap.util.varname(avoid, "pointers"),
           params="".join("," + x for x in params[1:]),
           numitems=oamap.util.varname(avoid, "numitems"),
           i=oamap.util.varname(avoid, "i"),
           range=rangename,
           len=lenname,
           j=oamap.util.varname(avoid, "j"),
           datum=oamap.util.varname(avoid, "datum"),
           fcn=fcnname), env)
        fill = oamap.util.trycompile(env[fillname], numba=numba)

        offsets = numpy.empty(len(viewstarts) + 1, dtype=oamap.generator.ListGenerator.posdtype)
        offsets[0] = 0
        pointers = numpy.empty(len(view), dtype=oamap.generator.PointerGenerator.posdtype)
        numitems = fill(*((view, viewstarts, viewstops, offsets[1:], pointers) + args))
        pointers = pointers[:numitems]

        listnode.content = oamap.schema.Pointer(listnode.content)

        if isinstance(listgenerator.content, oamap.generator.PointerGenerator):
            if isinstance(listgenerator.content, oamap.generator.Masked):
                raise NotImplementedError("nullable; need to merge masks")
            innerpointers = listgenerator.content._getpositions(data._arrays, data._cache)
            pointers = innerpointers[pointers]
            listnode.content.target = listnode.content.target.target

        arrays = _DualSource(data._arrays, data._generator.namespaces())
        arrays.put(listnode, offsets[:-1], offsets[1:])
        arrays.put(listnode.content, pointers)
        return schema(arrays)

    else:
        raise TypeError("filter can only be applied to a top-level OAMap proxy (List, Record, Tuple)")

transformations["filter"] = filter

################################################################ define

def define(data, fieldname, fcn, args=(), at="", fieldtype=oamap.schema.Primitive(numpy.float64), numba=True):
    if not isinstance(args, tuple):
        try:
            args = tuple(args)
        except TypeError:
            args = (args,)

    if (isinstance(data, oamap.proxy.ListProxy) and data._whence == 0 and data._stride == 1) or (isinstance(data, oamap.proxy.Proxy) and data._index == 0):
        schema = data._generator.namedschema()
        nodes = schema.path(at, parents=True)
        while isinstance(nodes[0], oamap.schema.List):
            nodes = (nodes[0].content,) + nodes
        if not isinstance(nodes[0], oamap.schema.Record):
            raise TypeError("path {0} does not refer to a record:\n\n    {1}".format(repr(at), nodes[0].__repr__(indent="    ")))
        if len(nodes) < 2 or not isinstance(nodes[1], oamap.schema.List):
            raise TypeError("path {0} does not refer to a record in a list:\n\n    {1}".format(repr(at), nodes[-1].__repr__(indent="    ")))
        recordnode = nodes[0]
        listnode = nodes[1]
        if recordnode.nullable or listnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        recordnode[fieldname] = fieldtype.deepcopy()

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)
        viewstarts, viewstops = listgenerator._getstartsstops(data._arrays, data._cache)
        viewschema = listgenerator.namedschema()
        viewarrays = _DualSource(data._arrays, data._generator.namespaces())
        if numpy.array_equal(viewstarts[1:], viewstops[:-1]):
            viewarrays.put(viewschema, viewstarts[:1], viewstops[-1:])
        else:
            raise NotImplementedError("non-contiguous arrays: have to do some sort of concatenation")
        view = viewschema(viewarrays)

        params = fcn.__code__.co_varnames[:fcn.__code__.co_argcount]
        avoid = set(params)
        fcnname = oamap.util.varname(avoid, "fcn")
        fillname = oamap.util.varname(avoid, "fill")

        ptypes = oamap.util.paramtypes(args)
        if ptypes is not None:
            import numba as nb
            from oamap.compiler import typeof_generator
            ptypes = (typeof_generator(view._generator.content),) + ptypes
        fcn = oamap.util.trycompile(fcn, paramtypes=ptypes, numba=numba)
        rtype = oamap.util.returntype(fcn, ptypes)

        if isinstance(fieldtype, oamap.schema.Primitive) and not fieldtype.nullable:
            if rtype is not None:
                if rtype == nb.types.pyobject:
                    raise TypeError("numba could not prove that the function's output type is:\n\n    {0}".format(fieldtype.__repr__(indent="    ")))
                elif rtype != nb.from_dtype(fieldtype.dtype):
                    raise TypeError("function returns {0} but fieldtype is set to:\n\n    {1}".format(rtype, fieldtype.__repr__(indent="    ")))

            env = {fcnname: fcn}
            exec("""
def {fill}({view}, {primitive}{params}):
    {i} = 0
    for {datum} in {view}:
        {primitive}[{i}] = {fcn}({datum}{params})
        {i} += 1
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           primitive=oamap.util.varname(avoid, "primitive"),
           params="".join("," + x for x in params[1:]),
           i=oamap.util.varname(avoid, "i"),
           datum=oamap.util.varname(avoid, "datum"),
           fcn=fcnname), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)

            primitive = numpy.empty(len(view), dtype=fieldtype.dtype)
            fill(*((view, primitive) + args))

            arrays = _DualSource(data._arrays, data._generator.namespaces())
            arrays.put(recordnode[fieldname], primitive)
            return schema(arrays)

        elif isinstance(fieldtype, oamap.schema.Primitive):
            if rtype is not None:
                if rtype != nb.types.optional(nb.from_dtype(fieldtype.dtype)):
                    raise TypeError("function returns {0} but fieldtype is set to:\n\n    {1}".format(rtype, fieldtype.__repr__(indent="    ")))

            env = {fcnname: fcn}
            exec("""
def {fill}({view}, {primitive}, {mask}{params}):
    {i} = 0
    {numitems} = 0
    for {datum} in {view}:
        {tmp} = {fcn}({datum}{params})
        if {tmp} is None:
            {mask}[{i}] = {maskedvalue}
        else:
            {mask}[{i}] = {numitems}
            {primitive}[{numitems}] = {tmp}
            {numitems} += 1
        {i} += 1
    return {numitems}
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           primitive=oamap.util.varname(avoid, "primitive"),
           mask=oamap.util.varname(avoid, "mask"),
           params="".join("," + x for x in params[1:]),
           i=oamap.util.varname(avoid, "i"),
           numitems=oamap.util.varname(avoid, "numitems"),
           datum=oamap.util.varname(avoid, "datum"),
           tmp=oamap.util.varname(avoid, "tmp"),
           fcn=fcnname,
           maskedvalue=oamap.generator.Masked.maskedvalue), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)
            
            primitive = numpy.empty(len(view), dtype=fieldtype.dtype)
            mask = numpy.empty(len(view), dtype=oamap.generator.Masked.maskdtype)
            fill(*((view, primitive, mask) + args))

            arrays = _DualSource(data._arrays, data._generator.namespaces())
            arrays.put(recordnode[fieldname], primitive, mask)
            return schema(arrays)

        else:
            raise NotImplementedError("define not implemented for fieldtype:\n\n    {0}".format(fieldtype.__repr__(indent="    ")))

    else:
        raise TypeError("define can only be applied to a top-level OAMap proxy (List, Record, Tuple)")

transformations["define"] = define

################################################################ map

def map(data, fcn, args=(), at="", names=None, numba=True):
    if not isinstance(args, tuple):
        try:
            args = tuple(args)
        except TypeError:
            args = (args,)

    if (isinstance(data, oamap.proxy.ListProxy) and data._whence == 0 and data._stride == 1) or (isinstance(data, oamap.proxy.Proxy) and data._index == 0):
        listnode = data._generator.namedschema().path(at)
        if not isinstance(listnode, oamap.schema.List):
            raise TypeError("path {0} does not refer to a list:\n\n    {1}".format(repr(at), listnode.__repr__(indent="    ")))
        if listnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)

        viewstarts, viewstops = listgenerator._getstartsstops(data._arrays, data._cache)
        viewschema = listgenerator.namedschema()
        viewarrays = _DualSource(data._arrays, data._generator.namespaces())
        viewoffsets = numpy.array([viewstarts.min(), viewstops.max()], dtype=oamap.generator.ListGenerator.posdtype)
        viewarrays.put(viewschema, viewoffsets[:1], viewoffsets[-1:])
        view = viewschema(viewarrays)

        params = fcn.__code__.co_varnames[:fcn.__code__.co_argcount]
        avoid = set(params)
        fcnname = oamap.util.varname(avoid, "fcn")
        fillname = oamap.util.varname(avoid, "fill")

        ptypes = oamap.util.paramtypes(args)
        if ptypes is not None:
            import numba as nb
            from oamap.compiler import typeof_generator
            ptypes = (typeof_generator(view._generator.content),) + ptypes
        fcn = oamap.util.trycompile(fcn, paramtypes=ptypes, numba=numba)
        rtype = oamap.util.returntype(fcn, ptypes)

        if rtype is None:
            viewindex = 0
            for datum in view:
                first = fcn(*((datum,) + args))
                viewindex += 1
                if first is not None:
                    break

            if viewindex == len(view):
                out = None

            else:
                if isinstance(first, numbers.Real):
                    out = numpy.empty(len(view), dtype=(numpy.int64 if isinstance(first, numbers.Integral) else numpy.float64))

                elif isinstance(first, tuple) and len(first) > 0 and all(isinstance(x, (numbers.Real, bool, numpy.bool_)) for x in first):
                    if names is None:
                        names = ["f" + str(i) for i in range(len(first))]
                    if len(names) != len(first):
                        raise TypeError("names has length {0} but function returns {1} numbers per row".format(len(names), len(first)))

                    out = numpy.empty(len(view), dtype=zip(names, [numpy.bool_ if isinstance(x, (bool, numpy.bool_)) else numpy.int64 if isinstance(x, numbers.Integral) else numpy.float64 for x in first]))

                else:
                    raise TypeError("function must return tuples of numbers (rows of a table)")

                numitems = 0
                out[numitems] = first
                numitems += 1
                if args == ():
                    for datum in view[viewindex:]:
                        tmp = fcn(datum)
                        if tmp is not None:
                            out[numitems] = tmp
                            numitems += 1
                else:
                    for datum in view[viewindex:]:
                        tmp = fcn(*((datum,) + args))
                        if tmp is not None:
                            out[numitems] = tmp
                            numitems += 1

                out = out[:numitems]
                        
        elif isinstance(rtype, (nb.types.Integer, nb.types.Float, nb.types.Boolean)):
            out = numpy.empty(len(view), dtype=numpy.dtype(rtype.name))
            env = {fcnname: fcn}
            exec("""
def {fill}({view}, {out}{params}):
    {numitems} = 0
    for {datum} in {view}:
        {out}[{numitems}] = {fcn}({datum}{params})
        {numitems} += 1
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           out=oamap.util.varname(avoid, "out"),
           params="".join("," + x for x in params[1:]),
           numitems=oamap.util.varname(avoid, "numitems"),
           datum=oamap.util.varname(avoid, "datum"),
           fcn=fcnname), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)
            fill(*((view, out) + args))

        elif isinstance(rtype, nb.types.Optional) and isinstance(rtype.type, (nb.types.Integer, nb.types.Float, nb.types.Boolean)):
            out = numpy.empty(len(view), dtype=numpy.dtype(rtype.type.name))
            env = {fcnname: fcn}
            exec("""
def {fill}({view}, {out}{params}):
    {numitems} = 0
    for {datum} in {view}:
        {tmp} = {fcn}({datum}{params})
        if {tmp} is not None:
            {out}[{numitems}] = {tmp}
            {numitems} += 1
    return {numitems}
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           out=oamap.util.varname(avoid, "out"),
           params="".join("," + x for x in params[1:]),
           numitems=oamap.util.varname(avoid, "numitems"),
           datum=oamap.util.varname(avoid, "datum"),
           tmp=oamap.util.varname(avoid, "tmp"),
           fcn=fcnname), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)
            numitems = fill(*((view, out) + args))
            out = out[:numitems]

        elif isinstance(rtype, (nb.types.Tuple, nb.types.UniTuple)) and len(rtype.types) > 0 and all(isinstance(x, (nb.types.Integer, nb.types.Float, nb.types.Boolean)) for x in rtype.types):
            if names is None:
                names = ["f" + str(i) for i in range(len(rtype.types))]
            if len(names) != len(rtype.types):
                raise TypeError("names has length {0} but function returns {1} numbers per row".format(len(names), len(rtype.types)))

            out = numpy.empty(len(view), dtype=zip(names, [numpy.dtype(x.name) for x in rtype.types]))
            outs = tuple(out[n] for n in names)

            outnames = [oamap.util.varname(avoid, "out" + str(i)) for i in range(len(names))]
            numitemsname = oamap.util.varname(avoid, "numitems")
            tmpname = oamap.util.varname(avoid, "tmp")
            env = {fcnname: fcn}
            exec("""
def {fill}({view}, {outs}{params}):
    {numitems} = 0
    for {datum} in {view}:
        {tmp} = {fcn}({datum}{params})
        {assignments}
        {numitems} += 1
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           outs=",".join(outnames),
           params="".join("," + x for x in params[1:]),
           numitems=numitemsname,
           datum=oamap.util.varname(avoid, "datum"),
           tmp=tmpname,
           fcn=fcnname,
           assignments="\n        ".join("{out}[{numitems}] = {tmp}[{i}]".format(out=out, numitems=numitemsname, tmp=tmpname, i=i) for i, out in enumerate(outnames))), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)
            fill(*((view,) + outs + args))

        elif isinstance(rtype, nb.types.Optional) and isinstance(rtype.type, (nb.types.Tuple, nb.types.UniTuple)) and len(rtype.type.types) > 0 and all(isinstance(x, (nb.types.Integer, nb.types.Float, nb.types.Boolean)) for x in rtype.type.types):
            if names is None:
                names = ["f" + str(i) for i in range(len(rtype.type.types))]
            if len(names) != len(rtype.type.types):
                raise TypeError("names has length {0} but function returns {1} numbers per row".format(len(names), len(rtype.type.types)))

            out = numpy.empty(len(view), dtype=zip(names, [numpy.dtype(x.name) for x in rtype.type.types]))
            outs = tuple(out[n] for n in names)

            outnames = [oamap.util.varname(avoid, "out" + str(i)) for i in range(len(names))]
            numitemsname = oamap.util.varname(avoid, "numitems")
            tmp2name = oamap.util.varname(avoid, "tmp2")
            requiredname = oamap.util.varname(avoid, "required")
            env = {fcnname: fcn, requiredname: oamap.compiler.required}
            exec("""
def {fill}({view}, {outs}{params}):
    {numitems} = 0
    for {datum} in {view}:
        {tmp} = {fcn}({datum}{params})
        if {tmp} is not None:
            {tmp2} = {required}({tmp})
            {assignments}
            {numitems} += 1
    return {numitems}
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           outs=",".join(outnames),
           params="".join("," + x for x in params[1:]),
           numitems=numitemsname,
           datum=oamap.util.varname(avoid, "datum"),
           tmp=oamap.util.varname(avoid, "tmp"),
           tmp2=tmp2name,
           required=requiredname,
           fcn=fcnname,
           assignments="\n            ".join("{out}[{numitems}] = {tmp2}[{i}]".format(out=out, numitems=numitemsname, tmp2=tmp2name, i=i) for i, out in enumerate(outnames))), env)
            fill = oamap.util.trycompile(env[fillname], numba=numba)
            numitems = fill(*((view,) + outs + args))
            out = out[:numitems]

        else:
            raise TypeError("function must return tuples of numbers (rows of a table)")

        return out

    else:
        raise TypeError("map can only be applied to a top-level OAMap proxy (List, Record, Tuple)")

class MapCombiner(object):
    def __init__(self, futures):
        self._futures = futures
        self._result = None
    def result(self, timeout=None):
        if self._result is None:
            starttime = time.time()
            results = []
            for future in self._futures:
                if timeout is not None:
                    timeout = max(1e-6, timeout - (time.time() - starttime))
                results.append(future.result(timeout))
            self._result = numpy.concatenate(results)
        return self._result
    def done(self):
        return all(x.done() for x in self._futures)
    def exception(self, timeout=None):
        raise NotImplementedError
    def traceback(self, timeout=None):
        raise NotImplementedError

map.combiner = MapCombiner
del MapCombiner

actions["map"] = map

################################################################ reduce

def reduce(data, tally, fcn, args=(), at="", numba=True):
    if not isinstance(args, tuple):
        try:
            args = tuple(args)
        except TypeError:
            args = (args,)

    if (isinstance(data, oamap.proxy.ListProxy) and data._whence == 0 and data._stride == 1) or (isinstance(data, oamap.proxy.Proxy) and data._index == 0):
        listnode = data._generator.namedschema().path(at)
        if not isinstance(listnode, oamap.schema.List):
            raise TypeError("path {0} does not refer to a list:\n\n    {1}".format(repr(at), listnode.__repr__(indent="    ")))
        if listnode.nullable:
            raise NotImplementedError("nullable; need to merge masks")

        listgenerator = data._generator.findbynames("List", listnode.namespace, starts=listnode.starts, stops=listnode.stops)
        viewstarts, viewstops = listgenerator._getstartsstops(data._arrays, data._cache)
        viewschema = listgenerator.namedschema()
        viewarrays = _DualSource(data._arrays, data._generator.namespaces())
        viewoffsets = numpy.array([viewstarts.min(), viewstops.max()], dtype=oamap.generator.ListGenerator.posdtype)
        viewarrays.put(viewschema, viewoffsets[:1], viewoffsets[-1:])
        view = viewschema(viewarrays)

        if fcn.__code__.co_argcount < 2:
            raise TypeError("function must have at least two parameters (data and tally)")

        params = fcn.__code__.co_varnames[:fcn.__code__.co_argcount]
        avoid = set(params)
        fcnname = oamap.util.varname(avoid, "fcn")
        fillname = oamap.util.varname(avoid, "fill")
        tallyname = params[1]

        ptypes = oamap.util.paramtypes(args)
        if ptypes is not None:
            import numba as nb
            from oamap.compiler import typeof_generator
            ptypes = (typeof_generator(view._generator.content), nb.typeof(tally)) + ptypes
        fcn = oamap.util.trycompile(fcn, paramtypes=ptypes, numba=numba)
        rtype = oamap.util.returntype(fcn, ptypes)

        if rtype is not None:
            if nb.typeof(tally) != rtype:
                raise TypeError("function should return the same type as tally")

        env = {fcnname: fcn}
        exec("""
def {fill}({view}, {tally}{params}):
    for {datum} in {view}:
        {tally} = {fcn}({datum}, {tally}{params})
    return {tally}
""".format(fill=fillname,
           view=oamap.util.varname(avoid, "view"),
           tally=tallyname,
           params="".join("," + x for x in params[2:]),
           datum=oamap.util.varname(avoid, "datum"),
           fcn=fcnname), env)
        fill = oamap.util.trycompile(env[fillname], numba=numba)

        return fill(*((view, tally) + args))

    else:
        raise TypeError("reduce can only be applied to a top-level OAMap proxy (List, Record, Tuple)")

class ReduceCombiner(object):
    def __init__(self, futures):
        self._futures = futures
        self._result = None
    def result(self, timeout=None):
        if self._result is None:
            starttime = time.time()
            result = None
            for future in self._futures:
                if timeout is not None:
                    timeout = max(1e-6, timeout - (time.time() - starttime))
                if result is None:
                    result = future.result(timeout)
                else:
                    result = result + future.result(timeout)
            self._result = result
        return self._result
    def done(self):
        return all(x.done() for x in self._futures)
    def exception(self, timeout=None):
        raise NotImplementedError
    def traceback(self, timeout=None):
        raise NotImplementedError

reduce.combiner = ReduceCombiner
del ReduceCombiner

actions["reduce"] = reduce
