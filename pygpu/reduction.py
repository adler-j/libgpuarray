import math

from mako.template import Template

from tools import ArrayArg, check_args, prod
from elemwise import parse_c_args, massage_op

import numpy
import gpuarray


basic_kernel = Template("""
${preamble}

#define REDUCE(a, b) (${reduce_expr})

KERNEL void ${name}(const unsigned int n, ${out_arg.decltype()} out
% for d in range(nd):
                    , const unsigned int dim${d}
% endfor
% for arg in arguments:
    % if arg.isarray():
                    , ${arg.decltype()} ${arg.name}_data
                    , const unsigned int ${arg.name}_offset
        % for d in range(nd):
                    , const int ${arg.name}_str_${d}
        % endfor
    % else:
                    , ${arg.decltype()} ${arg.name}
    % endif
% endfor
) {
  LOCAL_MEM ${out_arg.ctype()} ldata[${local_size}];
  const unsigned int lid = LID_0;
  unsigned int i;
  GLOBAL_MEM char *tmp;

% for arg in arguments:
  % if arg.isarray():
  tmp = (GLOBAL_MEM char *)${arg.name}_data; tmp += ${arg.name}_offset;
  ${arg.name}_data = (${arg.decltype()})tmp;
  % endif
% endfor

  i = GID_0;
% for i in range(nd-1, -1, -1):
  % if not redux[i]:
    % if i > 0:
  const unsigned int pos${i} = i % dim${i};
  i = i / dim${i};
    % else:
  const unsigned int pos${i} = i;
    % endif
  % endif
% endfor

  ${out_arg.ctype()} acc = ${neutral};

  for (i = lid; i < n; i += LDIM_0) {
    int ii = i;
    int pos;
% for arg in arguments:
    % if arg.isarray():
        GLOBAL_MEM char *${arg.name}_p = (GLOBAL_MEM char *)${arg.name}_data;
    % endif
% endfor
% for i in range(nd-1, -1, -1):
    % if redux[i]:
        % if i > 0:
        pos = ii % dim${i};
        ii = ii / dim${i};
        % else:
        pos = ii;
        % endif
        % for arg in arguments:
            % if arg.isarray():
        ${arg.name}_p += pos * ${arg.name}_str_${i};
            % endif
        % endfor
    % else:
        % for arg in arguments:
            % if arg.isarray():
        ${arg.name}_p += pos${i} * ${arg.name}_str_${i};
            % endif
        % endfor
    % endif
% endfor
% for arg in arguments:
    % if arg.isarray():
    ${arg.decltype()} ${arg.name} = (${arg.decltype()})${arg.name}_p;
    % endif
% endfor
    acc = REDUCE((acc), (${map_expr}));
  }
  ldata[lid] = acc;

  <% cur_size = local_size %>
  % while cur_size > 1:
    <% cur_size = cur_size / 2 %>
    local_barrier();
    if (lid < ${cur_size}) {
      ldata[lid] = REDUCE(ldata[lid], ldata[lid+${cur_size}]);
    }
  % endwhile
  if (lid == 0) out[GID_0] = ldata[0];
}
""")


class ReductionKernel(object):
    def __init__(self, context, dtype_out, neutral, reduce_expr, redux,
                 map_expr=None, arguments=None, preamble=""):
        self.context = context
        self.neutral = neutral
        self.redux = tuple(redux)
        if not any(self.redux):
            raise ValueError("Reduction is along no axes")
        self.dtype_out = dtype_out
        self.out_arg = ArrayArg(numpy.dtype(self.dtype_out), 'out')

        if isinstance(arguments, str):
            self.arguments = parse_c_args(arguments)
        elif arguments is None:
            self.arguments = [ArrayArg(numpy.dtype(self.dtype_out), '_reduce_input')]
        else:
            self.arguments = arguments

        self.reduce_expr = reduce_expr
        if map_expr is None:
            if len(self.arguments) != 1:
                raise ValueError("Don't know what to do with more than one "
                                 "argument. Specify map_expr to explicitly "
                                 "state what you want.")
            self.operation = "%s[i]" % (self.arguments[0].name,)
            self.expression = "%s[0]" % (self.arguments[0].name,)
        else:
            self.operation = map_expr
            self.expression = massage_op(map_expr)

        if not any(isinstance(arg, ArrayArg) for arg in self.arguments):
            raise ValueError("ReductionKernel can only be used with "
                             "functions that have at least one vector "
                             "argument.")
        
        have_small = False
        have_double = False
        have_complex = False
        for arg in self.arguments:
            if arg.dtype.itemsize < 4 and type(arg) == ArrayArg:
                have_small = True
            if arg.dtype in [numpy.float64, numpy.complex128]:
                have_double = True
            if arg.dtype in [numpy.complex64, numpy.complex128]:
                have_complex = True

        self.flags = dict(have_small=have_small, have_double=have_double,
                          have_complex=have_complex)
        self.preamble = preamble

        self.init_local_size = min(context.lmemsize //
                                   self.out_arg.dtype.itemsize,
                                   context.maxlsize)

    def _find_kernel_ls(self, tmpl, max_ls, *tmpl_args):
        local_size = min(self.init_local_size, max_ls)
        # nearest power of 2 (going up)
        count_lim = int(math.ceil(math.log(local_size, 2)))
        local_size = 2**count_lim
        loop_count = 0
        while loop_count <= count_lim:
            k = tmpl(local_size, *tmpl_args)

            if local_size <= k.maxlsize:
                return k, local_size
            else:
                local_size /= 2

            loop_count += 1

        raise RuntimeError("Can't stabilize the local_size for kernel."
                           " Please report this along with your "
                           "reduction code.")

    def _gen_basic(self, ls, nd):
        src = basic_kernel.render(preamble=self.preamble,
                                  reduce_expr=self.reduce_expr,
                                  name="reduk",
                                  out_arg=self.out_arg,
                                  nd=nd, arguments=self.arguments,
                                  local_size=ls,
                                  redux=self.redux,
                                  neutral=self.neutral,
                                  map_expr=self.expression)
        k = gpuarray.GpuKernel(src, "reduk", context=self.context,
                               cluda=True, **self.flags)
        return k

    def _get_basic_kernel(self, maxls, nd):
        return self._find_kernel_ls(self._gen_basic, maxls, nd)

    def _call_basic(self, n, args, offsets):
        gs = self._get_gs(n, self.contig_ls, self.contig_k)
        out = self._alloc_out(gs)
        kernel_args = [numpy.asarray(n, dtype='uint32'), out]
        for i, arg in enumerate(args):
            kernel_args.append(arg)
            if isinstance(arg, gpuarray.GpuArray):
                kernel_args.append(numpy.asarray(offsets[i], dtype='uint32'))
        self.contig_k(*kernel_args, ls=self.contig_ls, gs=gs)
        return gs, out

    def __call__(self, *args, **kwargs):
        _, nd, dims, strs, offsets, contig = check_args(args, collapse=False,
                                                        broadcast=False)
        out = kwargs.pop('out', None)
        if len(kwargs) != 0:
            raise TypeError('Unexpected keyword argument: %s' %
                            kwargs.keys()[0])
        n = prod(dims)
        out_shape = tuple(d for i, d in enumerate(dims) if not self.redux[i])
        gs = prod(out_shape)
        n /= gs
        if gs > self.context.maxgsize:
            raise ValueError("Array to big to be reduced along the "
                             "selected axes")


        if out is None:
            out = gpuarray.empty(out_shape, context=self.context,
                                 dtype=self.dtype_out)
        else:
            assert out.shape == out_shape
        k, ls = self._get_basic_kernel(n, nd)

        kargs = [numpy.asarray(n, dtype='uint32'), out]
        kargs.extend(numpy.asarray(d, dtype='uint32') for d in dims)
        for i, arg in enumerate(args):
            if isinstance(arg, gpuarray.GpuArray):
                kargs.append(arg)
                kargs.append(numpy.asarray(offsets[i], dtype='uint32'))
                kargs.extend(numpy.asarray(s, dtype='int32') for s in strs[i])
            else:
                kargs.append(arg)

        print ls, gs
        k(*kargs, ls=ls, gs=gs)

        return out
