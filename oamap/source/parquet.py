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

import gzip
import math
import os
import struct
import sys
import zlib

import numpy

import oamap.schema
import oamap.generator
import oamap.source._fastparquet.schema
import oamap.source._fastparquet.core
from oamap.source._fastparquet.thrift import thriftpy
from oamap.source._fastparquet.thrift import parquet_thrift

class ParquetFile(object):
    def __init__(self, file):
        if parquet_thrift is None:
            raise ImportError("\n\nTo read Parquet files, install thriftpy package with:\n\n    pip install thriftpy --user\nor\n    conda install -c conda-forge thriftpy")

        if isinstance(file, numpy.ndarray):
            if file.dtype.itemsize != 1 or len(file.shape) != 1:
                raise TypeError("if file is a Numpy array, the item size must be 1 (such as numpy.uint8) and shape must be flat, not {0} and {1}".format(file.dtype, file.shape))
            self.memmap = file
        elif hasattr(file, "read") and hasattr(file, "seek"):
            self.file = file
        else:
            raise TypeError("file must be a Numpy array (e.g. memmap) or a file-like object with read and seek methods")

        if hasattr(self, "memmap"):
            headermagic = self.memmap[:4].tostring()
            footermagic = self.memmap[-4:].tostring()
            footerbytes, = self.memmap[-8:-4].view("<i4")
            index = len(self.memmap) - (footerbytes + 8)

            class TFileTransport(thriftpy.transport.TTransportBase):
                def __init__(self, memmap, index):
                    self._memmap = memmap
                    self._index = index
                def _read(self, bytes):
                    if not (0 <= self._index < len(self._memmap)):
                        raise IOError("seek point {0} is beyond array with {1} bytes".format(self._index, len(self._memmap)))
                    out = self._memmap[self._index : self._index + bytes]
                    self._index += bytes
                    if len(out) == 0:
                        return b""
                    else:
                        return out.tostring()

            self.TFileTransport = TFileTransport

        else:
            self.file.seek(0, os.SEEK_SET)
            headermagic = file.read(4)

            self.file.seek(-4, os.SEEK_END)
            footermagic = file.read(4)

            self.file.seek(-8, os.SEEK_END)
            footerbytes, = struct.unpack(b"<i", file.read(4))

            self.file.seek(-(footerbytes + 8), os.SEEK_END)
            index = None

            class TFileTransport(thriftpy.transport.TTransportBase):
                def __init__(self, file, index):
                    self._file = file
                def _read(self, bytes):
                    return self._file.read(bytes)

            self.TFileTransport = TFileTransport

        if headermagic != b"PAR1":
            raise ValueError("not a Parquet-formatted file: header magic is {0}".format(repr(headermagic)))
        if footermagic != b"PAR1":
            raise ValueError("not a Parquet-formatted file: footer magic is {0}".format(repr(footermagic)))

        tin = self.TFileTransport(file, index)
        pin = thriftpy.protocol.compact.TCompactProtocolFactory().get_protocol(tin)
        self.footer = parquet_thrift.FileMetaData()
        self.footer.read(pin)

        def recurse(index, path):
            schema = self.footer.schema[index]
            schema.path = path + [schema.name]
            if schema.num_children is not None:
                for i in range(schema.num_children):
                    index += 1
                    index = recurse(index, schema.path)
            return index

        index = 0
        self.fields = []
        while index + 1 < len(self.footer.schema):
            index += 1
            self.fields.append(self.footer.schema[index])
            index = recurse(index, [])

        self.schema_helper = oamap.source._fastparquet.schema.SchemaHelper(self.footer.schema)

    def column(self, rowgroupid, schema, parallel=False):
        if parallel:
            raise NotImplementedError

        print "schema.path", schema.path
        found = False
        for column in self.footer.row_groups[rowgroupid].columns:
            print "column.meta_data.path_in_schema", column.meta_data.path_in_schema
            if column.meta_data.path_in_schema == schema.path:
                found = True
                break
        if not found:
            raise AssertionError("columnpath not found: {0}".format(columnpath))

        def get_num_values(header):
            if header.type == parquet_thrift.PageType.DATA_PAGE:
                return header.data_page_header.num_values
            elif header.type == parquet_thrift.PageType.INDEX_PAGE:
                return header.index_page_header.num_values
            elif header.type == parquet_thrift.PageType.DICTIONARY_PAGE:
                return header.dictionary_page_header.num_values
            elif header.type == parquet_thrift.PageType.DATA_PAGE_V2:
                return header.data_page_header_v2.num_values
            else:
                raise AssertionError("unrecognized header type: {0}".format(header.type))

        if hasattr(self, "memmap"):
            def pagereader(index):
                # always safe for parallelization
                num_values = 0
                while num_values < column.meta_data.num_values:
                    tin = self.TFileTransport(self.memmap, index)
                    pin = thriftpy.protocol.compact.TCompactProtocolFactory().get_protocol(tin)
                    header = parquet_thrift.PageHeader()
                    header.read(pin)
                    index = tin._index
                    compressed = self.memmap[index : index + header.compressed_page_size]
                    index += header.compressed_page_size
                    num_values += get_num_values(header)
                    yield header, compressed

        else:
            def pagereader(index):
                # if parallel, open a new file to avoid conflicts with other threads
                file = self.file
                file.seek(index, os.SEEK_SET)
                num_values = 0
                while num_values < column.meta_data.num_values:
                    tin = self.TFileTransport(file, index)
                    pin = thriftpy.protocol.compact.TCompactProtocolFactory().get_protocol(tin)
                    header = parquet_thrift.PageHeader()
                    header.read(pin)
                    compressed = file.read(header.compressed_page_size)
                    num_values += get_num_values(header)
                    yield header, compressed
        
        decompress = _decompression(column.meta_data.codec)
        
        dictionary = None
        deflevelsegs = []
        replevelsegs = []
        datasegs = []

        for header, compressed in pagereader(column.file_offset):
            uncompressed = numpy.frombuffer(decompress(compressed, header.compressed_page_size, header.uncompressed_page_size), dtype=numpy.uint8)

            # data page
            if header.type == parquet_thrift.PageType.DATA_PAGE:
                deflevelseg, replevelseg, dataseg = oamap.source._fastparquet.core.read_data_page(uncompressed, self.schema_helper, header, column.meta_data)
                
                if deflevelseg is not None:
                    deflevelsegs.append(deflevelseg)
                if replevelseg is not None:
                    replevelsegs.append(replevelseg)
                if dataseg is not None:
                    datasegs.append(dataseg)

            # index page (doesn't exist in Parquet yet, either)
            elif header.type == parquet_thrift.PageType.INDEX_PAGE:
                raise NotImplementedError

            # dictionary page
            elif header.type == parquet_thrift.PageType.DICTIONARY_PAGE:
                dictionary = oamap.source._fastparquet.core.read_dictionary_page(uncompressed, self.schema_helper, header, column.meta_data)
                # dictionary = _plain(uncompressed, column.meta_data, header.dictionary_page_header.num_values)

            # data page version 2
            elif header.type == parquet_thrift.PageType.DATA_PAGE_V2:
                raise NotImplementedError

            else:
                raise AssertionError("unrecognized header type: {0}".format(header.type))

        if len(deflevelsegs) == 0:
            deflevel = None
        elif len(deflevelsegs) == 1:
            deflevel = deflevelsegs[0]
        else:
            deflevel = numpy.concatenate(deflevelsegs)

        if len(replevelsegs) == 0:
            replevel = None
        elif len(replevelsegs) == 1:
            replevel = replevelsegs[0]
        else:
            replevel = numpy.concatenate(replevelsegs)

        if len(datasegs) == 0:
            data = None
        elif len(datasegs) == 1:
            data = datasegs[0]
        else:
            data = numpy.concatenate(datasegs)

        return dictionary, deflevel, replevel, data

try:
    import snappy
except ImportError:
    snappy = None
try:
    import lzo
except ImportError:
    lzo = None
try:
    import brotli
except ImportError:
    brotli = None
try:
    import lz4.block
except ImportError:
    lz4 = None

def _decompression(codec):
    if codec == parquet_thrift.CompressionCodec.UNCOMPRESSED:
        return lambda compressed, compressedbytes, uncompressedbytes: compressed

    elif codec == parquet_thrift.CompressionCodec.SNAPPY:
        if snappy is None:
            raise ImportError("\n\nTo read Parquet files with snappy compression, install snappy package with:\n\n    pip install python-snappy --user\nor\n    conda install -c conda-forge python-snappy")
        return lambda compressed, compressedbytes, uncompressedbytes: snappy.decompress(compressed)

    elif codec == parquet_thrift.CompressionCodec.GZIP:
        # return gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb").read()
        if sys.version_info[0] <= 2:
            return lambda compressed, compressedbytes, uncompressedbytes: zlib.decompress(compressed, 16 + 15)
        else:
            return lambda compressed, compressedbytes, uncompressedbytes: gzip.decompress(compressed)

    elif codec == parquet_thrift.CompressionCodec.LZO:
        if lzo is None:
            raise ImportError("install lzo")      # FIXME: provide installation instructions
        else:
            return lambda compressed, compressedbytes, uncompressedbytes: lzo.decompress(compressed)

    elif codec == parquet_thrift.CompressionCodec.BROTLI:
        if brotli is None:
            raise ImportError("install brotli")   # FIXME: provide installation instructions
        else:
            return lambda compressed, compressedbytes, uncompressedbytes: brotli.decompress(compressed)

    elif codec == parquet_thrift.CompressionCodec.LZ4:
        if lz4 is None:
            raise ImportError("\n\nTo read Parquet files with lz4 compression, install lz4 package with:\n\n    pip install lz4 --user\nor\n    conda install -c anaconda lz4")
        else:
            return lambda compressed, compressedbytes, uncompressedbytes: lz4.block.decompress(compressed, uncompressed_size=uncompressedbytes)

    elif codec == parquet_thrift.CompressionCodec.ZSTD:
        # FIXME: find the Python zstd package
        raise NotImplementedError("ZSTD decompression")

    else:
        raise AssertionError("unrecognized codec: {0}".format(codec))

###############################









###############################

                # # interpret definition levels, if any
                # num_nulls = 0
                # if not schema.required:
                #     bitwidth = int(math.ceil(math.log(schema.maxdef + 1, 2)))
                #     if bitwidth > 0:
                #         deflevelseg, index = _interpret(uncompressed, header.data_page_header.num_values, bitwidth, header.data_page_header.definition_level_encoding)
                #         num_nulls = numpy.count_nonzero(deflevelseg != schema.maxdef)
                #         print "deflevelseg", deflevelseg, "num_nulls", num_nulls

                # # interpret repetition levels, if any
                # if len(schema.path) > 1:
                #     bitwidth = int(math.ceil(math.log(schema.maxrep + 1, 2)))
                #     if bitwidth > 0:
                #         replevelseg, i = _interpret(uncompressed[index:], header.data_page_header.num_values, bitwidth, header.data_page_header.repetition_level_encoding)
                #         index += i
                #         print "replevelseg", replevelseg

                # # interpret the data (plain)
                # if header.data_page_header.encoding == parquet_thrift.Encoding.PLAIN:
                #     dataseg = _plain(uncompressed[index:], column.meta_data, header.data_page_header.num_values - num_nulls)

                # # interpret the data (plain dictionary)
                # elif header.data_page_header.encoding == parquet_thrift.Encoding.PLAIN_DICTIONARY or header.data_page_header.encoding == parquet_thrift.Encoding.RLE:
                #     if header.data_page_header.encoding == parquet_thrift.Encoding.RLE:
                #         bitwidth = schema.type_length
                #     else:
                #         bitwidth = uncompressed[index]
                #         index += 1
                #     print "bitwidth", bitwidth
                #     print "data", uncompressed[index:]

                #     out = numpy.empty(header.data_page_header.num_values - num_nulls, dtype=numpy.int32)
                #     print "BEFORE", out
                #     _interpret_rle_bitpacked_hybrid(uncompressed[index:], out, bitwidth)
                #     print "AFTER", out


                #     raise NotImplementedError




                # else:
                #     raise AssertionError("unexpected encoding: {0}".format(header.data_page_header.encoding))

# def _plain(data, metadata, num_values):
#     if metadata.type == parquet_thrift.Type.BOOLEAN:
#         return numpy.unpackbits(data).reshape((-1, 8))[:,::-1].ravel().astype(numpy.bool_)[:num_values]

#     elif metadata.type == parquet_thrift.Type.INT32:
#         return data.view("<i4")

#     elif metadata.type == parquet_thrift.Type.INT64:
#         return data.view("<i8")

#     elif metadata.type == parquet_thrift.Type.INT96:
#         return data.view("S12")

#     elif metadata.type == parquet_thrift.Type.FLOAT:
#         return data.view("<f4")

#     elif metadata.type == parquet_thrift.Type.DOUBLE:
#         return data.view("<f8")

#     elif metadata.type == parquet_thrift.Type.BYTE_ARRAY:
#         raise NotImplementedError

#     elif metadata.type == parquet_thrift.Type.FIXED_LEN_BYTE_ARRAY:
#         raise NotImplementedError

#     else:
#         raise AssertionError("unrecognized column type: {0}".format(metadata.type))

# def _interpret(data, count, bitwidth, encoding):
#     out = numpy.empty(count, dtype=numpy.int32)

#     if encoding == parquet_thrift.Encoding.RLE:
#         return _interpret_rle_bitpacked_hybrid(data, out, bitwidth)

#     elif encoding == parquet_thrift.Encoding.BIT_PACKED:
#         raise NotImplementedError

#     else:
#         raise AssertionError("unexpected encoding: {0}".format(encoding))

# def _interpret_rle_bitpacked_hybrid(data, out, bitwidth):
#     index = 0
#     outdex = 0
#     while outdex < len(out):
#         length = data[index] + data[index + 1]*256 + data[index + 2]*256*256 + data[index + 3]*256*256*256
#         index += 4

#         start = index
#         while index - start < length and outdex < len(out):
#             header, index = _interpret_unsigned_varint(data, index)
#             if header & 1 == 0:
#                 index, outdex = _interpret_rle(data, index, header, bitwidth, out, outdex)
#             else:
#                 index, outdex = _interpret_bitpacked(data, index, header, bitwidth, out, outdex)
#         index = start + length

#     return out, index

# def _interpret_unsigned_varint(data, index):
#     out = 0
#     shift = 0
#     while True:
#         byte = data[index]
#         index += 1
#         out |= ((byte & 0x7f) << shift)
#         if (byte & 0x80) == 0:
#             break
#         shift += 7
#     return out, index

# def _interpret_rle(data, index, header, bitwidth, out, outdex):
#     print "_interpret_rle"

#     count = (header >> 1)
#     width = (bitwidth + 7) // 8
#     zero = numpy.zeros(4, dtype=numpy.int8)
#     zero[:width] = data[:width]
#     index += width
#     value = zero.view(numpy.int32)
#     out[outdex : outdex + count] = value
#     outdex += count
#     return index, outdex

# def _interpret_bitpacked(data, index, header, bitwidth, out, outdex):
#     print "_interpret_bitpacked"

#     num_groups = header >> 1
#     count = num_groups * 8
#     byte_count = (bitwidth * count) // 8
#     raw_bytes = data[index : index + byte_count]
#     index += byte_count
#     mask = (1 << bitwidth) - 1
#     current_byte = 0
#     byte = raw_bytes[current_byte]
#     bits_wnd_l = 8
#     bits_wnd_r = 0
#     total = byte_count * 8
#     while total >= bitwidth:
#         if bits_wnd_r >= 8:
#             bits_wnd_r -= 8
#             bits_wnd_l -= 8
#             byte >>= 8
#         elif bits_wnd_l - bits_wnd_r >= bitwidth:
#             if outdex < len(out):
#                 out[outdex] = (byte >> bits_wnd_r) & mask
#                 outdex += 1
#             total -= bitwidth
#             bits_wnd_r += bitwidth
#         elif current_byte + 1 < byte_count:
#             current_byte += 1
#             byte |= (raw_bytes[current_byte] << bits_wnd_l)
#             bits_wnd_l += 8
#     return index, outdex

# # if numba is not None:
# #     njit = numba.jit(nopython=True, nogil=True)
# #     _interpret_rle_bitpacked_hybrid = njit(_interpret_rle_bitpacked_hybrid)
# #     _interpret_unsigned_varint      = njit(_interpret_unsigned_varint)
# #     _interpret_rle                  = njit(_interpret_rle)
# #     _interpret_bitpacked            = njit(_interpret_bitpacked)