from argparse import ArgumentParser, Namespace
from typing import Optional

import lief
from miasm.core import parse_asm
from miasm.core.asmblock import AsmCFG, asm_resolve_final
from miasm.core.interval import interval

from themida_unmutate.miasm_utils import MiasmContext
from themida_unmutate.symbolic_execution import disassemble_and_simplify_functions
from themida_unmutate.unwrapping import unwrap_function

NEW_SECTION_NAME = ".unmut"
NEW_SECTION_MAX_SIZE = 2**16


def entry_point() -> None:
    # Parse command-line arguments
    args = parse_arguments()
    protected_func_addrs = list(map(lambda addr: int(addr, 0), args.addresses))

    # Setup disassembler and lifter
    miasm_ctx = MiasmContext(args.protected_binary)

    # Resolve mutated functions' addresses
    mutated_func_addrs = unwrap_functions(args.protected_binary,
                                          protected_func_addrs)

    # Disassemble mutated functions and simplify them
    simplified_func_asmcfgs = disassemble_and_simplify_functions(
        miasm_ctx, mutated_func_addrs)

    # Map protected functions' addresses to their corresponding simplified `AsmCFG`
    func_addr_to_simplified_cfg = {
        protected_func_addrs[i]: asm_cfg
        for i, asm_cfg in enumerate(simplified_func_asmcfgs)
    }

    # Rewrite the protected binary with simplified functions
    rebuild_simplified_binary(miasm_ctx, func_addr_to_simplified_cfg,
                              args.protected_binary, args.output)


def parse_arguments() -> Namespace:
    """
    Parse command-line arguments.
    """
    parser = ArgumentParser("Automatic deobfuscation tool powered by Miasm")
    parser.add_argument("protected_binary", help="Protected binary path")
    parser.add_argument("-a",
                        "--addresses",
                        nargs='+',
                        help="Addresses of the functions to deobfuscate",
                        required=True)
    parser.add_argument("-o",
                        "--output",
                        help="Output binary path",
                        required=True)

    return parser.parse_args()


def unwrap_functions(target_binary_path: str,
                     target_function_addrs: list[int]) -> list[int]:
    """
    Resolve mutated function's addresses from original function addresses.
    """
    mutated_func_addrs: list[int] = []
    for addr in target_function_addrs:
        print(f"Resolving mutated code portion address for 0x{addr:x}...")
        mutated_code_addr = unwrap_function(target_binary_path, addr)
        if mutated_code_addr == addr:
            raise Exception("Failure to unwrap function")

        print(f"Mutated code is at 0x{mutated_code_addr:x}")
        mutated_func_addrs.append(mutated_code_addr)

    return mutated_func_addrs


def rebuild_simplified_binary(
    miasm_ctx: MiasmContext,
    func_addr_to_simplified_cfg: dict[int, AsmCFG],
    input_binary_path: str,
    output_binary_path: str,
) -> None:
    """
    Reassemble functions' `AsmCFG` and rewrite the input binary with simplified
    machine code.
    """
    if len(func_addr_to_simplified_cfg) == 0:
        raise ValueError("`protected_function_addrs` cannot be empty")

    # Open the target binary with LIEF
    pe_obj = lief.PE.parse(input_binary_path)
    if pe_obj is None:
        raise Exception(f"Failed to parse PE '{input_binary_path}'")

    # Create a new code section
    unmut_section = lief.PE.Section(
        [0] * NEW_SECTION_MAX_SIZE, NEW_SECTION_NAME,
        lief.PE.SECTION_CHARACTERISTICS.CNT_CODE.value
        | lief.PE.SECTION_CHARACTERISTICS.MEM_READ.value
        | lief.PE.SECTION_CHARACTERISTICS.MEM_EXECUTE.value)
    pe_obj.add_section(unmut_section)
    unmut_section = pe_obj.get_section(NEW_SECTION_NAME)

    image_base = pe_obj.imagebase
    unmut_section_base = image_base + unmut_section.virtual_address

    # Reassemble simplified AsmCFGs
    original_to_simplified: dict[int, int] = {}
    next_min_offset_for_asm = 0
    unmut_section_patches: list[tuple[int, bytes]] = []
    for protected_func_addr, simplified_asmcfg in \
            func_addr_to_simplified_cfg.items():
        # Unpin blocks to be able to relocate the whole CFG
        head = simplified_asmcfg.heads()[0]
        for ir_block in simplified_asmcfg.blocks:
            miasm_ctx.loc_db.unset_location_offset(ir_block.loc_key)

        # Relocate the function's entry block
        miasm_ctx.loc_db.set_location_offset(
            head, unmut_section_base + next_min_offset_for_asm)

        # Generate the simplified machine code
        new_section_patches = asm_resolve_final(
            miasm_ctx.mdis.arch,
            simplified_asmcfg,
            dst_interval=interval([
                (unmut_section_base + next_min_offset_for_asm,
                 unmut_section_base + unmut_section.virtual_size -
                 next_min_offset_for_asm)
            ]))

        # Merge patches into the patch list
        for patch in new_section_patches.items():
            unmut_section_patches.append(patch)

        # Associate original addr to simplified addr
        original_to_simplified[protected_func_addr] = min(
            new_section_patches.keys())
        next_min_offset_for_asm = max(
            new_section_patches.keys()) - unmut_section_base + 15

    # Overwrite the section's content
    new_section_size = next_min_offset_for_asm
    new_content = bytearray([0] * new_section_size)
    for addr, data in unmut_section_patches:
        offset = addr - unmut_section_base
        new_content[offset:offset + len(data)] = data
    unmut_section.content = memoryview(new_content)

    # Find the section containing the virtual addresses we want to modify
    protected_function_addrs = func_addr_to_simplified_cfg.keys()
    target_rva = next(iter(protected_function_addrs)) - image_base
    text_section = section_from_virtual_address(pe_obj, target_rva)
    assert text_section is not None

    # Redirect functions to their simplified versions
    unmut_jmp_patches: list[tuple[int, bytes]] = []
    for target_addr in protected_function_addrs:
        # Generate a single-block AsmCFG with a JMP to the simplified version
        simplified_func_addr = original_to_simplified[target_addr]
        original_loc_str = f"loc_{target_addr:x}"
        jmp_unmut_instr_str = f"{original_loc_str}:\nJMP 0x{simplified_func_addr:x}"
        jmp_unmut_asmcfg = parse_asm.parse_txt(miasm_ctx.mdis.arch,
                                               miasm_ctx.mdis.attrib,
                                               jmp_unmut_instr_str,
                                               miasm_ctx.mdis.loc_db)

        # Unpin loc_key if it's pinned
        original_loc = miasm_ctx.loc_db.get_offset_location(target_addr)
        if original_loc is not None:
            miasm_ctx.loc_db.unset_location_offset(original_loc)

        # Relocate the newly created block and generate machine code
        original_loc = miasm_ctx.loc_db.get_name_location(original_loc_str)
        miasm_ctx.loc_db.set_location_offset(original_loc, target_addr)
        new_jmp_patches = asm_resolve_final(miasm_ctx.mdis.arch,
                                            jmp_unmut_asmcfg)

        # Merge patches into the patch list
        for patch in new_jmp_patches.items():
            unmut_jmp_patches.append(patch)

    # Apply patches
    text_section_base = image_base + text_section.virtual_address
    text_section_bytes = bytearray(text_section.content)
    for addr, data in unmut_jmp_patches:
        offset = addr - text_section_base
        text_section_bytes[offset:offset + len(data)] = data
    text_section.content = memoryview(text_section_bytes)

    # Invoke the builder
    builder = lief.PE.Builder(pe_obj)
    builder.build()

    # Save the result
    builder.write(output_binary_path)


def section_from_virtual_address(lief_bin: lief.Binary,
                                 virtual_addr: int) -> Optional[lief.Section]:
    for s in lief_bin.sections:
        if s.virtual_address <= virtual_addr < s.virtual_address + s.size:
            assert isinstance(s, lief.Section)
            return s

    return None


if __name__ == "__main__":
    entry_point()