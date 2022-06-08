import subprocess

from ctypes import sizeof
from typing import List

import igvm.elf as elflib

from igvm.bootcstruct import *
from igvm.acpi import ACPI, ACPI_RSDP_ADDR
from igvm.igvmbase import IGVMBaseGenerator
from igvm.igvmfile import PGSIZE, ALIGN
from igvm.vmstate import ARCH
boot_params = struct_boot_params
setup_header = struct_setup_header

class IGVMELFGenerator(IGVMBaseGenerator):

    def __init__(self, **kwargs):
        # Parse BzImage header
        IGVMBaseGenerator.__init__(self, **kwargs)
        self.extra_validated_ram: List = []
        self._start = kwargs["start_addr"]

        acpi_dir = kwargs["acpi_dir"] if "acpi_dir" in kwargs else None
        self.acpidata: ACPI = ACPI(acpi_dir)

        self.elf = elflib.ELFObj(self.infile)

        in_path = self.infile.name
        bin_path = in_path + ".binary"
        subprocess.check_output(["objcopy", in_path, "-O", "binary", bin_path])
        with open(bin_path, "rb") as f:
            self._kernel: bytes = f.read()

        self._vmpl2_kernel_file: argparse.FileType = kwargs["vmpl2_kernel"]
        self._vmpl2_kernel: bytearray = bytearray(self._vmpl2_kernel_file.read())
        # Create a setup_header for 32-bit
        print(self._vmpl2_header.setup_sects)
        self._header = struct_setup_header()
        self._header.init_size = ALIGN(len(self._kernel), PGSIZE)
        self._header.pref_address = self._start

    @property
    def _vmpl2_header(self) -> setup_header:
        header = setup_header.from_buffer(self._vmpl2_kernel, 0x1f1)
        print(header.setup_sects)
        assert header.header.to_bytes(
            4, 'little') == b'HdrS', 'invalid setup_header'
        assert header.pref_address > 3 * 1024 * 1024, 'loading base cannot be below 3MB'
        assert header.xloadflags & 1, '64-bit entrypoint does not exist'
        assert header.pref_address % PGSIZE == 0
        assert header.init_size % PGSIZE == 0
        return header

    def setup_before_code(self):
        # [0-0xa0000] is reserved for BIOS
        # [0xe0000 - 0x200000] is for ACPI related data
        # load ACPI pages
        acpi_tables = self.acpidata
        sorted_gpa = sorted(acpi_tables.acpi.keys())
        # RAM for bios/bootloader
        self.state.seek(0xa0000)
        self.state.memory.allocate(acpi_tables.end_addr - 0xa0000)
        for gpa in sorted_gpa:
            self.state.memory.write(gpa, acpi_tables.acpi[gpa])
        self.state.seek(acpi_tables.end_addr)
        return sorted_gpa[0]

    def load_code(self):
        self.state.seek(self._start)
        self.state.memory.allocate(len(self._kernel), PGSIZE)
        self.state.memory.write(self._start, self._kernel)
        entry_offset = self.elf.elf.header.e_entry - \
            self.elf.elf.get_section_by_name(".text").header.sh_addr
        return self._start + entry_offset

    def load_vmpl2_kernel(self):
        self.state.seek(0x2d00000)
        self.state.memory.allocate(len(self._vmpl2_kernel), PGSIZE)
        self.state.memory.write(0x2d00000, self._vmpl2_kernel)

    def setup_after_code(self, kernel_entry: int):
        addr = self.state.setup_paging(paging_level = 4)
        self.state.setup_gdt()
        self.state.setup_idt()
        boot_params_addr = self.state.memory.allocate(
            sizeof(struct_boot_params))
        boot_stack_addr = self.state.memory.allocate(2 * PGSIZE)
        end = self.state.memory.allocate(0)
        self.extra_validated_ram.append((addr, end-addr))
        params = struct_boot_params.from_buffer(
            self.state.memory, boot_params_addr)
        params.hdr = self._header
        params.acpi_rsdp_addr = ACPI_RSDP_ADDR
        params.e820_entries = self._setup_e820_opt(params.e820_table)
        del params  # kill reference to re-allow allocation
        self.state.vmsa.rip = kernel_entry
        self.state.vmsa.rsi = boot_params_addr
        self.state.vmsa.rsp = boot_stack_addr + PGSIZE
        self.load_vmpl2_kernel()

    def _setup_e820_opt(self, e820_table):
        e820_table[0].addr = 0
        e820_table[0].size = 0
        e820_table[0].type = E820_TYPE_RAM
        e820_table[1].addr = 0xa0000
        e820_table[1].size = 0x100000 - 0xa0000
        e820_table[1].type = E820_TYPE_RESERVED
        e820_table[2].addr = 0x100000
        e820_table[2].size = self.acpidata.end_addr - 0x100000
        e820_table[2].type = E820_TYPE_ACPI
        e820_table[3].addr = self.SNP_CPUID_PAGE_ADDR
        e820_table[3].size = 4 * PGSIZE
        e820_table[3].type = E820_TYPE_RESERVED
        e820_table[4].addr = self._start
        e820_table[4].size = len(self._kernel)
        e820_table[4].type = E820_TYPE_RAM
        count = 5
        for addr, size in self.extra_validated_ram:
            e820_table[count].addr = addr
            e820_table[count].size = size
            e820_table[count].type = E820_TYPE_RAM
            count += 1
        e820_table[count].addr = 0x2d00000
        e820_table[count].size = len(self._vmpl2_kernel)
        e820_table[count].type = E820_TYPE_RESERVED
        count += 1
        for i in range(count):
            print("%x %x"%(e820_table[i].addr, e820_table[i].addr + e820_table[i].size))

        return count
